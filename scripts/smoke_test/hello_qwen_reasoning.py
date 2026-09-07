#!/usr/bin/env python3
"""Run Qwen3.5 Thinking and print its reasoning trace and final answer separately.

The reasoning trace printed here is text emitted by the model. It should not be
assumed to be a complete or faithful description of the model's internal
computation.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Sequence


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.qwen_runtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_PROMPT,
    DEFAULT_REASONING_END_MARKER,
    choose_dtype,
    configure_cache,
    dtype_load_kwargs,
    input_device_for,
    split_reasoning_output,
)


@contextmanager
def timed_stage(
    timings: list[tuple[str, float]],
    label: str,
    synchronize: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Measure a stage, synchronizing CUDA around asynchronous GPU work."""
    if synchronize is not None:
        synchronize()
    started = time.perf_counter()
    try:
        yield
    finally:
        if synchronize is not None:
            synchronize()
        timings.append((label, time.perf_counter() - started))


def print_timing_breakdown(
    timings: Sequence[tuple[str, float]], total_seconds: float
) -> None:
    """Print stage durations and their share of end-to-end wall time."""
    measured_seconds = sum(seconds for _, seconds in timings)
    unaccounted_seconds = max(0.0, total_seconds - measured_seconds)
    rows = [*timings, ("Other Python/printing overhead", unaccounted_seconds)]
    label_width = max(len(label) for label, _ in rows)

    print("\n=== Timing breakdown ===")
    print(f"{'Stage':<{label_width}}  {'Seconds':>10}  {'% total':>8}")
    print(f"{'-' * label_width}  {'-' * 10}  {'-' * 8}")
    for label, seconds in rows:
        percentage = 100 * seconds / total_seconds if total_seconds > 0 else 0.0
        print(f"{label:<{label_width}}  {seconds:10.3f}  {percentage:7.1f}%")
    print(f"{'Total wall time':<{label_width}}  {total_seconds:10.3f}  {100.0:7.1f}%")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="User prompt sent to the model.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=32_768,
        help="Maximum generated tokens, including reasoning (default: 32768).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.6,
        help="Sampling temperature (default: 0.6).",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.95,
        help="Nucleus-sampling cutoff (default: 0.95).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Top-k sampling cutoff (default: 20).",
    )
    parser.add_argument(
        "--min-p",
        type=float,
        default=0.0,
        help="Minimum-token-probability cutoff (default: 0.0).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed (default: 0).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Hugging Face cache root; defaults to persistent RunPod storage.",
    )
    parser.add_argument(
        "--reasoning-end-marker",
        default=DEFAULT_REASONING_END_MARKER,
        help='Marker separating reasoning from the final answer (default: "</think>").',
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.max_new_tokens <= 0:
        raise SystemExit("--max-new-tokens must be greater than 0.")
    if args.temperature <= 0:
        raise SystemExit(
            "--temperature must be greater than 0 for this thinking model."
        )
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be greater than 0.")
    if not 0 <= args.min_p <= 1:
        raise SystemExit("--min-p must be in [0, 1].")
    if not args.reasoning_end_marker:
        raise SystemExit("--reasoning-end-marker must not be empty.")


def main() -> int:
    total_started = time.perf_counter()
    timings: list[tuple[str, float]] = []

    with timed_stage(timings, "Arguments and cache setup"):
        args = parse_args()
        validate_args(args)
        cache_root = configure_cache(args.cache_dir)

    try:
        with timed_stage(timings, "Import PyTorch/Hugging Face"):
            import torch
            import transformers
            from huggingface_hub import snapshot_download
            from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        print("Missing dependency. Run ./setup.sh first.", file=sys.stderr)
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

    with timed_stage(timings, "CUDA initialization"):
        if not torch.cuda.is_available():
            print(
                "CUDA is unavailable. Run this script inside a RunPod PyTorch GPU pod.",
                file=sys.stderr,
            )
            return 2

        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        dtype = choose_dtype(torch)
        gpu_index = torch.cuda.current_device()
        gpu = torch.cuda.get_device_properties(gpu_index)
        torch.cuda.synchronize()

    with timed_stage(timings, "Print environment information"):
        print("=== Environment ===")
        print(f"PyTorch:      {torch.__version__}")
        print(f"Transformers: {transformers.__version__}")
        print(f"GPU:          {gpu.name} ({gpu.total_memory / (1024**3):.1f} GiB)")
        print(f"Precision:    {str(dtype).removeprefix('torch.')}")
        print(f"HF cache:     {cache_root}")
        print(f"Model:        {args.model}")
        print("Timing note: the Hub stage includes downloads on a cache miss.")
        sys.stdout.flush()

    with timed_stage(timings, "Hub download/cache resolution"):
        snapshot_path = snapshot_download(
            repo_id=args.model,
            cache_dir=os.environ["HF_HUB_CACHE"],
        )

    with timed_stage(timings, "Load tokenizer from disk"):
        tokenizer = AutoTokenizer.from_pretrained(
            snapshot_path,
            local_files_only=True,
        )

    try:
        with timed_stage(
            timings,
            "Load model weights onto GPU",
            synchronize=torch.cuda.synchronize,
        ):
            model = AutoModelForCausalLM.from_pretrained(
                snapshot_path,
                device_map="auto",
                low_cpu_mem_usage=True,
                local_files_only=True,
                **dtype_load_kwargs(transformers.__version__, dtype),
            )
            model.eval()
    except torch.cuda.OutOfMemoryError:
        print(
            "CUDA ran out of memory while loading the model. Use a larger GPU or "
            "a smaller/quantized model.",
            file=sys.stderr,
        )
        print_timing_breakdown(timings, time.perf_counter() - total_started)
        return 3

    with timed_stage(timings, "Format and tokenize prompt"):
        model_inputs = tokenizer.apply_chat_template(
            [{"role": "user", "content": args.prompt}],
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )

    with timed_stage(
        timings,
        "Move prompt tensors to GPU",
        synchronize=torch.cuda.synchronize,
    ):
        input_device = input_device_for(model)
        model_inputs = {
            key: value.to(input_device) if hasattr(value, "to") else value
            for key, value in model_inputs.items()
        }
    prompt_tokens = int(model_inputs["input_ids"].shape[-1])

    generation_kwargs: dict[str, Any] = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": True,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "min_p": args.min_p,
        "use_cache": True,
    }
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        generation_kwargs["pad_token_id"] = tokenizer.eos_token_id

    print("\nGenerating reasoning and final answer...")
    try:
        with timed_stage(
            timings,
            "Autoregressive generation",
            synchronize=torch.cuda.synchronize,
        ):
            with torch.inference_mode():
                generated_ids = model.generate(**model_inputs, **generation_kwargs)
    except torch.cuda.OutOfMemoryError:
        print(
            "CUDA ran out of memory. Use a larger GPU, fewer generated tokens, "
            "or a smaller/quantized model.",
            file=sys.stderr,
        )
        print_timing_breakdown(timings, time.perf_counter() - total_started)
        return 3

    generation_seconds = dict(timings)["Autoregressive generation"]

    with timed_stage(
        timings,
        "Transfer generated tokens to CPU",
        synchronize=torch.cuda.synchronize,
    ):
        output_ids = generated_ids[0, prompt_tokens:].tolist()

    with timed_stage(timings, "Split and decode model output"):
        (
            reasoning,
            final_answer,
            found_marker,
            reasoning_tokens,
            final_answer_tokens,
        ) = split_reasoning_output(output_ids, tokenizer, args.reasoning_end_marker)

    with timed_stage(timings, "Print reasoning and final answer"):
        print("\n=== Reasoning trace (model-emitted) ===")
        print(reasoning or "[No reasoning text was generated.]")
        print("\n=== Final answer ===")
        if found_marker:
            print(
                final_answer
                or "[The model emitted no text after the reasoning marker.]"
            )
        else:
            print(
                "[No final answer could be separated because the model did not emit "
                f"{args.reasoning_end_marker!r}. The reasoning may have been truncated.]"
            )

        print("\n=== Generation stats ===")
        print(f"Prompt tokens:     {prompt_tokens}")
        print(f"Reasoning tokens:  {reasoning_tokens}")
        print(f"Final-answer tokens: {final_answer_tokens}")
        print(f"All generated tokens: {len(output_ids)}")
        print(f"Generation time:   {generation_seconds:.3f} s")
        if generation_seconds > 0:
            print(
                f"Generation throughput: "
                f"{len(output_ids) / generation_seconds:.2f} tokens/s"
            )
        sys.stdout.flush()

    total_seconds = time.perf_counter() - total_started
    print_timing_breakdown(timings, total_seconds)

    print(
        "\nNote: reasoning and final-answer generation happen inside one "
        "generate() call, so their compute times are not separately observable "
        "without changing the generation procedure. Their token counts are "
        "reported above."
    )

    return 0 if found_marker else 4


if __name__ == "__main__":
    raise SystemExit(main())
