#!/usr/bin/env python3
"""Batch Qwen reasoning inference with PyTorch/Transformers.

Normal mode samples 50 answers in mini-batches and writes one CSV plus one
Markdown file. ``--benchmark`` tests several batch sizes using the same loaded
model, without requiring vLLM or any dependencies beyond this repository's
existing setup.
"""

from __future__ import annotations

import argparse
import csv
import gc
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Sequence

from hello_qwen_reasoning import (
    DEFAULT_MODEL,
    DEFAULT_PROMPT,
    DEFAULT_REASONING_END_MARKER,
    choose_dtype,
    configure_cache,
    dtype_load_kwargs,
    input_device_for,
    split_reasoning_output,
)


DEFAULT_BATCH_SIZES = (1, 2, 4, 8, 16)


@dataclass(frozen=True)
class SampleResult:
    sample_number: int
    prompt: str
    reasoning: str
    answer: str
    reasoning_complete: bool
    raw_response: str
    reasoning_tokens: int
    answer_tokens: int
    generated_tokens: int


@dataclass(frozen=True)
class GenerationMetrics:
    samples: int
    generated_tokens: int
    seconds: float

    @property
    def samples_per_second(self) -> float:
        return self.samples / self.seconds if self.seconds > 0 else 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.generated_tokens / self.seconds if self.seconds > 0 else 0.0


@dataclass(frozen=True)
class BenchmarkResult:
    batch_size: int
    status: str
    metrics: GenerationMetrics | None = None
    peak_memory_gib: float | None = None


def parse_batch_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "batch sizes must be comma-separated integers"
        ) from exc
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("batch sizes must be positive integers")
    return tuple(dict.fromkeys(sizes))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--num-samples", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument(
        "--reasoning-end-marker",
        default=DEFAULT_REASONING_END_MARKER,
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=None,
        help=(
            "Output path without an extension. The script adds .csv and .md; "
            "the default is a timestamped name under outputs/."
        ),
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Benchmark candidate batch sizes instead of saving generations.",
    )
    parser.add_argument(
        "--benchmark-batch-sizes",
        type=parse_batch_sizes,
        default=DEFAULT_BATCH_SIZES,
        metavar="SIZES",
        help="Comma-separated batch sizes (default: 1,2,4,8,16).",
    )
    parser.add_argument(
        "--benchmark-samples",
        type=int,
        default=16,
        help="Samples generated for each benchmark candidate (default: 16).",
    )
    parser.add_argument(
        "--benchmark-max-new-tokens",
        type=int,
        default=256,
        help="Fixed generated tokens per benchmark sample (default: 256).",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.prompt.strip():
        raise SystemExit("--prompt must not be empty.")
    for name in (
        "num_samples",
        "batch_size",
        "max_new_tokens",
        "benchmark_samples",
        "benchmark_max_new_tokens",
    ):
        if getattr(args, name) <= 0:
            option = "--" + name.replace("_", "-")
            raise SystemExit(f"{option} must be greater than 0.")
    if args.temperature <= 0:
        raise SystemExit("--temperature must be greater than 0.")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
    if not args.reasoning_end_marker:
        raise SystemExit("--reasoning-end-marker must not be empty.")
    if args.benchmark and not any(
        size <= args.benchmark_samples for size in args.benchmark_batch_sizes
    ):
        raise SystemExit(
            "At least one --benchmark-batch-sizes value must not exceed "
            "--benchmark-samples."
        )


def load_runtime(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Load the same cached model stack used by hello_qwen_reasoning.py."""
    cache_root = configure_cache(args.cache_dir)
    try:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run ./setup.sh first.") from exc

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is unavailable. Run this script inside a RunPod PyTorch GPU pod."
        )

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    dtype = choose_dtype(torch)
    gpu = torch.cuda.get_device_properties(torch.cuda.current_device())

    print("=== Environment ===")
    print(f"PyTorch:      {torch.__version__}")
    print(f"Transformers: {transformers.__version__}")
    print(f"GPU:          {gpu.name} ({gpu.total_memory / (1024**3):.1f} GiB)")
    print(f"Precision:    {str(dtype).removeprefix('torch.')}")
    print(f"HF cache:     {cache_root}")
    print(f"Model:        {args.model}")

    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        cache_dir=os.environ["HF_HUB_CACHE"],
    )
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise RuntimeError(
                "The tokenizer has neither a pad token nor an EOS token."
            )
        tokenizer.pad_token = tokenizer.eos_token
    # Decoder-only generation requires left padding when prompt lengths differ.
    tokenizer.padding_side = "left"

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        cache_dir=os.environ["HF_HUB_CACHE"],
        device_map="auto",
        low_cpu_mem_usage=True,
        **dtype_load_kwargs(transformers.__version__, dtype),
    )
    model.eval()
    return torch, tokenizer, model


def prepare_inputs(
    tokenizer: Any, prompt: str, batch_size: int, input_device: Any
) -> Any:
    messages = [[{"role": "user", "content": prompt}] for _ in range(batch_size)]
    texts = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    # Chat templates already contain the model's required special tokens.
    return tokenizer(
        texts,
        return_tensors="pt",
        padding=True,
        add_special_tokens=False,
    ).to(input_device)


def eos_token_ids(model: Any, tokenizer: Any) -> set[int]:
    configured = getattr(model.generation_config, "eos_token_id", None)
    if configured is None:
        configured = tokenizer.eos_token_id
    if configured is None:
        return set()
    if isinstance(configured, int):
        return {configured}
    return {int(token_id) for token_id in configured}


def trim_generated_tokens(token_ids: Sequence[int], eos_ids: set[int]) -> list[int]:
    """Remove padding after the first generated EOS while retaining that EOS."""
    result = [int(token_id) for token_id in token_ids]
    for index, token_id in enumerate(result):
        if token_id in eos_ids:
            return result[: index + 1]
    return result


def generate_in_batches(
    *,
    torch: Any,
    tokenizer: Any,
    model: Any,
    prompt: str,
    num_samples: int,
    batch_size: int,
    generation_kwargs: dict[str, Any],
    collect_outputs: bool,
    show_progress: bool,
) -> tuple[list[list[int]], GenerationMetrics]:
    """Run every mini-batch and return generated token IDs plus timing."""
    input_device = input_device_for(model)
    endings = eos_token_ids(model, tokenizer)
    collected: list[list[int]] = []
    total_generated_tokens = 0

    torch.cuda.synchronize()
    started = time.perf_counter()
    for start in range(0, num_samples, batch_size):
        current_size = min(batch_size, num_samples - start)
        if show_progress:
            end = start + current_size
            print(f"Generating samples {start + 1}-{end}/{num_samples}...")

        inputs = prepare_inputs(tokenizer, prompt, current_size, input_device)
        input_width = int(inputs["input_ids"].shape[1])
        with torch.inference_mode():
            sequences = model.generate(**inputs, **generation_kwargs)

        generated = sequences[:, input_width:].detach().cpu().tolist()
        for row in generated:
            trimmed = trim_generated_tokens(row, endings)
            total_generated_tokens += len(trimmed)
            if collect_outputs:
                collected.append(trimmed)

        del inputs, sequences, generated

    torch.cuda.synchronize()
    seconds = time.perf_counter() - started
    return collected, GenerationMetrics(
        samples=num_samples,
        generated_tokens=total_generated_tokens,
        seconds=seconds,
    )


def generation_kwargs(
    args: argparse.Namespace,
    tokenizer: Any,
    *,
    max_new_tokens: int,
    fixed_length: bool = False,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "do_sample": True,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_new_tokens": max_new_tokens,
        "use_cache": True,
        "pad_token_id": tokenizer.pad_token_id,
    }
    if fixed_length:
        # Equal output lengths make candidate batch sizes directly comparable.
        options["min_new_tokens"] = max_new_tokens
    return options


def parse_sample(
    token_ids: Sequence[int],
    *,
    sample_number: int,
    prompt: str,
    tokenizer: Any,
    marker: str,
) -> SampleResult:
    parsed = split_reasoning_output(token_ids, tokenizer, marker)
    reasoning, answer, complete = parsed[:3]
    if len(parsed) >= 5:
        reasoning_tokens, answer_tokens = int(parsed[3]), int(parsed[4])
    else:
        reasoning_tokens = len(token_ids) if not complete else 0
        answer_tokens = 0
    raw_response = tokenizer.decode(token_ids, skip_special_tokens=False).strip()
    return SampleResult(
        sample_number=sample_number,
        prompt=prompt,
        reasoning=reasoning,
        answer=answer,
        reasoning_complete=complete,
        raw_response=raw_response,
        reasoning_tokens=reasoning_tokens,
        answer_tokens=answer_tokens,
        generated_tokens=len(token_ids),
    )


def output_paths(prefix: Path | None) -> tuple[Path, Path]:
    if prefix is None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        prefix = Path.cwd() / "outputs" / f"qwen_batch_{stamp}"
    else:
        prefix = prefix.expanduser()
        if not prefix.is_absolute():
            prefix = Path.cwd() / prefix
    csv_path = Path(f"{prefix}.csv").resolve()
    markdown_path = Path(f"{prefix}.md").resolve()
    existing = [path for path in (csv_path, markdown_path) if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite existing output: "
            + ", ".join(str(path) for path in existing)
        )
    return csv_path, markdown_path


def fenced_text(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def render_csv(
    results: Sequence[SampleResult], model_name: str, batch_size: int
) -> str:
    destination = StringIO(newline="")
    fields = [
        "sample_number",
        "model",
        "batch_size",
        "prompt",
        "reasoning",
        "answer",
        "reasoning_complete",
        "raw_response",
        "reasoning_tokens",
        "answer_tokens",
        "generated_tokens",
    ]
    writer = csv.DictWriter(destination, fieldnames=fields)
    writer.writeheader()
    for result in results:
        writer.writerow(
            {
                "sample_number": result.sample_number,
                "model": model_name,
                "batch_size": batch_size,
                "prompt": result.prompt,
                "reasoning": result.reasoning,
                "answer": result.answer,
                "reasoning_complete": result.reasoning_complete,
                "raw_response": result.raw_response,
                "reasoning_tokens": result.reasoning_tokens,
                "answer_tokens": result.answer_tokens,
                "generated_tokens": result.generated_tokens,
            }
        )
    return destination.getvalue()


def render_markdown(
    results: Sequence[SampleResult],
    *,
    model_name: str,
    batch_size: int,
    metrics: GenerationMetrics,
) -> str:
    destination = StringIO()
    print("# Qwen batched inference", file=destination)
    print(file=destination)
    print(f"- Model: `{model_name}`", file=destination)
    print(f"- Samples: {len(results)}", file=destination)
    print(f"- Batch size: {batch_size}", file=destination)
    print(f"- Time: {metrics.seconds:.3f} s", file=destination)
    print(f"- Samples/s: {metrics.samples_per_second:.3f}", file=destination)
    print(f"- Generated tokens/s: {metrics.tokens_per_second:.2f}", file=destination)
    print("\n## Prompt\n", file=destination)
    print(fenced_text(results[0].prompt), file=destination)

    for result in results:
        print(f"\n---\n\n## Answer {result.sample_number}\n", file=destination)
        if result.reasoning_complete:
            print(
                fenced_text(result.answer or "[No final answer emitted.]"),
                file=destination,
            )
        else:
            print(
                "The reasoning marker was not found; complete raw output:\n",
                file=destination,
            )
            print(fenced_text(result.raw_response), file=destination)
        print("\n<details>", file=destination)
        print("<summary>Model-emitted reasoning</summary>\n", file=destination)
        print(
            fenced_text(result.reasoning or "[No reasoning emitted.]"),
            file=destination,
        )
        print("\n</details>", file=destination)
    return destination.getvalue()


def write_outputs(
    results: Sequence[SampleResult],
    *,
    args: argparse.Namespace,
    metrics: GenerationMetrics,
    csv_path: Path,
    markdown_path: Path,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    csv_contents = render_csv(results, args.model, args.batch_size)
    markdown_contents = render_markdown(
        results,
        model_name=args.model,
        batch_size=args.batch_size,
        metrics=metrics,
    )
    with csv_path.open("x", encoding="utf-8", newline="") as output:
        output.write(csv_contents)
    try:
        with markdown_path.open("x", encoding="utf-8") as output:
            output.write(markdown_contents)
    except Exception:
        csv_path.unlink()
        raise


def is_cuda_oom(error: BaseException, torch: Any) -> bool:
    return isinstance(error, torch.cuda.OutOfMemoryError) or (
        "out of memory" in str(error).lower() and "cuda" in str(error).lower()
    )


def benchmark_batch_sizes(
    args: argparse.Namespace, torch: Any, tokenizer: Any, model: Any
) -> int:
    candidates = [
        size
        for size in args.benchmark_batch_sizes
        if size <= args.benchmark_samples
    ]
    skipped = [
        size
        for size in args.benchmark_batch_sizes
        if size > args.benchmark_samples
    ]
    if skipped:
        print(f"Skipping sizes larger than sample count: {skipped}")

    print("\nWarming up the model...")
    warmup_options = generation_kwargs(
        args,
        tokenizer,
        max_new_tokens=min(8, args.benchmark_max_new_tokens),
        fixed_length=True,
    )
    generate_in_batches(
        torch=torch,
        tokenizer=tokenizer,
        model=model,
        prompt=args.prompt,
        num_samples=1,
        batch_size=1,
        generation_kwargs=warmup_options,
        collect_outputs=False,
        show_progress=False,
    )

    options = generation_kwargs(
        args,
        tokenizer,
        max_new_tokens=args.benchmark_max_new_tokens,
        fixed_length=True,
    )
    results: list[BenchmarkResult] = []
    for batch_size in candidates:
        print(f"Benchmarking batch size {batch_size}...")
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        try:
            _, metrics = generate_in_batches(
                torch=torch,
                tokenizer=tokenizer,
                model=model,
                prompt=args.prompt,
                num_samples=args.benchmark_samples,
                batch_size=batch_size,
                generation_kwargs=options,
                collect_outputs=False,
                show_progress=False,
            )
            peak_memory = torch.cuda.max_memory_allocated() / (1024**3)
            results.append(
                BenchmarkResult(
                    batch_size=batch_size,
                    status="ok",
                    metrics=metrics,
                    peak_memory_gib=peak_memory,
                )
            )
        except RuntimeError as exc:
            if not is_cuda_oom(exc, torch):
                raise
            print(f"  batch size {batch_size} ran out of GPU memory")
            results.append(BenchmarkResult(batch_size=batch_size, status="OOM"))
            gc.collect()
            torch.cuda.empty_cache()

    print("\n=== Batch-size benchmark ===")
    print(
        f"{ 'Batch':>5}  {'Status':>6}  {'Seconds':>9}  "
        f"{'Samples/s':>10}  {'Tokens/s':>10}  {'Peak GiB':>8}"
    )
    print(f"{'-' * 5}  {'-' * 6}  {'-' * 9}  {'-' * 10}  {'-' * 10}  {'-' * 8}")
    successful: list[BenchmarkResult] = []
    for result in results:
        if result.metrics is None or result.peak_memory_gib is None:
            print(f"{result.batch_size:5d}  {result.status:>6}")
            continue
        successful.append(result)
        print(
            f"{result.batch_size:5d}  {result.status:>6}  "
            f"{result.metrics.seconds:9.3f}  "
            f"{result.metrics.samples_per_second:10.3f}  "
            f"{result.metrics.tokens_per_second:10.2f}  "
            f"{result.peak_memory_gib:8.2f}"
        )

    if not successful:
        print("\nNo tested batch size fit in GPU memory.", file=sys.stderr)
        return 3

    fastest = max(successful, key=lambda item: item.metrics.samples_per_second)
    threshold = fastest.metrics.samples_per_second * 0.95
    recommended = min(
        (
            item
            for item in successful
            if item.metrics.samples_per_second >= threshold
        ),
        key=lambda item: item.batch_size,
    )
    print(
        "\nRecommended batch size: "
        f"{recommended.batch_size} (smallest batch within 95% of the best "
        "samples/s)."
    )
    print(f"Fastest measured batch size: {fastest.batch_size}.")
    print(
        "This recommendation applies to "
        f"{args.benchmark_max_new_tokens}-token outputs. Longer outputs use more "
        "KV-cache memory; rerun with a larger --benchmark-max-new-tokens for a "
        "closer match to production."
    )
    return 0


def run_generation(
    args: argparse.Namespace, torch: Any, tokenizer: Any, model: Any
) -> int:
    try:
        csv_path, markdown_path = output_paths(args.output_prefix)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    options = generation_kwargs(
        args,
        tokenizer,
        max_new_tokens=args.max_new_tokens,
    )
    try:
        token_batches, metrics = generate_in_batches(
            torch=torch,
            tokenizer=tokenizer,
            model=model,
            prompt=args.prompt,
            num_samples=args.num_samples,
            batch_size=args.batch_size,
            generation_kwargs=options,
            collect_outputs=True,
            show_progress=True,
        )
    except RuntimeError as exc:
        if not is_cuda_oom(exc, torch):
            raise
        print(
            "CUDA ran out of memory. Retry with a smaller --batch-size or fewer "
            "--max-new-tokens.",
            file=sys.stderr,
        )
        return 3

    results = [
        parse_sample(
            token_ids,
            sample_number=index,
            prompt=args.prompt,
            tokenizer=tokenizer,
            marker=args.reasoning_end_marker,
        )
        for index, token_ids in enumerate(token_batches, start=1)
    ]
    try:
        write_outputs(
            results,
            args=args,
            metrics=metrics,
            csv_path=csv_path,
            markdown_path=markdown_path,
        )
    except OSError as exc:
        print(f"Could not write outputs: {exc}", file=sys.stderr)
        return 5

    complete = sum(result.reasoning_complete for result in results)
    print("\n=== Complete ===")
    print(f"Samples:          {len(results)}")
    print(f"Complete splits:  {complete}/{len(results)}")
    print(f"Time:             {metrics.seconds:.3f} s")
    print(f"Samples/s:        {metrics.samples_per_second:.3f}")
    print(f"Generated tokens/s: {metrics.tokens_per_second:.2f}")
    print(f"CSV:              {csv_path}")
    print(f"Markdown:         {markdown_path}")
    if complete != len(results):
        print(
            "Warning: some generations did not emit the reasoning end marker; "
            "their raw output was still saved.",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    args = parse_args()
    validate_args(args)
    try:
        torch, tokenizer, model = load_runtime(args)
        if args.benchmark:
            return benchmark_batch_sizes(args, torch, tokenizer, model)
        return run_generation(args, torch, tokenizer, model)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
