#!/usr/bin/env python3
"""Run Qwen3 Thinking and print its reasoning trace and final answer separately.

The reasoning trace printed here is text emitted by the model. It should not be
assumed to be a complete or faithful description of the model's internal
computation.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MODEL = "Qwen/Qwen3-4B-Thinking-2507"
DEFAULT_PROMPT = (
    "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
    "ball. How much does the ball cost?"
)
DEFAULT_REASONING_END_MARKER = "</think>"


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
        raise SystemExit("--temperature must be greater than 0 for this thinking model.")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be greater than 0.")
    if not 0 <= args.min_p <= 1:
        raise SystemExit("--min-p must be in [0, 1].")
    if not args.reasoning_end_marker:
        raise SystemExit("--reasoning-end-marker must not be empty.")


def configure_cache(cache_dir: Path | None) -> Path:
    """Choose a persistent Hugging Face cache before importing Transformers."""
    if cache_dir is not None:
        chosen = cache_dir.expanduser().resolve()
        os.environ["HF_HOME"] = str(chosen)
    elif os.environ.get("HF_HOME"):
        chosen = Path(os.environ["HF_HOME"]).expanduser()
    else:
        runpod_volume = os.environ.get("RUNPOD_VOLUME_PATH")
        candidates = [Path(runpod_volume)] if runpod_volume else []
        candidates.append(Path("/workspace"))
        writable_root = next(
            (path for path in candidates if path.is_dir() and os.access(path, os.W_OK)),
            Path.home(),
        )
        chosen = writable_root / ".cache" / "huggingface"
        os.environ["HF_HOME"] = str(chosen)

    chosen.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_CACHE", str(chosen / "hub"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return chosen


def choose_dtype(torch_module: Any) -> Any:
    if torch_module.cuda.is_bf16_supported():
        return torch_module.bfloat16
    return torch_module.float16


def dtype_load_kwargs(transformers_version: str, dtype: Any) -> dict[str, Any]:
    """Use the non-deprecated dtype keyword when the installed version supports it."""
    numeric_parts: list[int] = []
    for part in transformers_version.split(".")[:2]:
        digits = "".join(character for character in part if character.isdigit())
        numeric_parts.append(int(digits) if digits else 0)
    version = tuple((numeric_parts + [0, 0])[:2])
    return {"dtype": dtype} if version >= (4, 56) else {"torch_dtype": dtype}


def input_device_for(model: Any) -> Any:
    embeddings = model.get_input_embeddings()
    if embeddings is not None and hasattr(embeddings, "weight"):
        return embeddings.weight.device
    return next(model.parameters()).device


def marker_token_ids(tokenizer: Any, marker: str) -> list[int]:
    marker_id = tokenizer.convert_tokens_to_ids(marker)
    if marker_id is not None and marker_id != tokenizer.unk_token_id:
        return [int(marker_id)]

    encoded = tokenizer.encode(marker, add_special_tokens=False)
    if not encoded:
        raise RuntimeError(f"Tokenizer could not encode reasoning marker {marker!r}.")
    return [int(token_id) for token_id in encoded]


def find_last_subsequence(tokens: Sequence[int], marker: Sequence[int]) -> int | None:
    """Return the start of the last marker occurrence, or None when absent."""
    if not marker or len(marker) > len(tokens):
        return None
    for start in range(len(tokens) - len(marker), -1, -1):
        if list(tokens[start : start + len(marker)]) == list(marker):
            return start
    return None


def split_reasoning_output(
    output_ids: Sequence[int], tokenizer: Any, marker: str
) -> tuple[str, str, bool]:
    """Split generated tokens into reasoning and final-answer text."""
    end_ids = marker_token_ids(tokenizer, marker)
    marker_start = find_last_subsequence(output_ids, end_ids)
    if marker_start is None:
        unparsed = tokenizer.decode(output_ids, skip_special_tokens=True).strip()
        return unparsed, "", False

    answer_start = marker_start + len(end_ids)
    reasoning = tokenizer.decode(
        output_ids[:answer_start], skip_special_tokens=True
    ).strip()
    answer = tokenizer.decode(output_ids[answer_start:], skip_special_tokens=True).strip()
    return reasoning, answer, True


def main() -> int:
    args = parse_args()
    validate_args(args)
    cache_root = configure_cache(args.cache_dir)

    try:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        print("Missing dependency. Run ./setup.sh first.", file=sys.stderr)
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

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
    print("=== Environment ===")
    print(f"PyTorch:      {torch.__version__}")
    print(f"Transformers: {transformers.__version__}")
    print(f"GPU:          {gpu.name} ({gpu.total_memory / (1024**3):.1f} GiB)")
    print(f"Precision:    {str(dtype).removeprefix('torch.')}")
    print(f"HF cache:     {cache_root}")
    print(f"Model:        {args.model}")

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        device_map="auto",
        low_cpu_mem_usage=True,
        **dtype_load_kwargs(transformers.__version__, dtype),
    )
    model.eval()

    model_inputs = tokenizer.apply_chat_template(
        [{"role": "user", "content": args.prompt}],
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
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
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            generated_ids = model.generate(**model_inputs, **generation_kwargs)
        torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError:
        print(
            "CUDA ran out of memory. Use a larger GPU, fewer generated tokens, "
            "or a smaller/quantized model.",
            file=sys.stderr,
        )
        return 3

    elapsed = time.perf_counter() - started
    output_ids = generated_ids[0, prompt_tokens:].tolist()
    reasoning, final_answer, found_marker = split_reasoning_output(
        output_ids, tokenizer, args.reasoning_end_marker
    )

    print("\n=== Reasoning trace (model-emitted) ===")
    print(reasoning or "[No reasoning text was generated.]")
    print("\n=== Final answer ===")
    if found_marker:
        print(final_answer or "[The model emitted no text after the reasoning marker.]")
    else:
        print(
            "[No final answer could be separated because the model did not emit "
            f"{args.reasoning_end_marker!r}. The reasoning may have been truncated.]"
        )

    print("\n=== Generation stats ===")
    print(f"Generated tokens: {len(output_ids)}")
    print(f"Generation time:  {elapsed:.2f} s")
    if elapsed > 0:
        print(f"Throughput:       {len(output_ids) / elapsed:.2f} tokens/s")

    return 0 if found_marker else 4


if __name__ == "__main__":
    raise SystemExit(main())
