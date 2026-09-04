#!/usr/bin/env python3
"""Generate many Qwen3.5 Thinking answers in one vLLM request.

By default, the script samples 50 answers to the same prompt and writes a
timestamped CSV/Markdown pair under ``outputs/``. The reasoning trace exposed
here is text emitted by the model; it should not be treated as a complete or
faithful account of the model's internal computation.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Sequence

DEFAULT_MODEL = "Qwen/Qwen3.5-4B"
DEFAULT_PROMPT = (
    "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
    "ball. How much does the ball cost?"
)
DEFAULT_REASONING_END_MARKER = "</think>"
DEFAULT_NUM_ANSWERS = 50


@dataclass(frozen=True)
class BatchAnswer:
    """One vLLM completion, including its parsed reasoning and final answer."""

    answer_number: int
    prompt: str
    reasoning: str
    answer: str
    reasoning_complete: bool
    raw_response: str
    finish_reason: str
    stop_reason: str
    generated_tokens: int


@dataclass(frozen=True)
class RunMetadata:
    """Settings and aggregate measurements saved with a batch."""

    model: str
    prompt: str
    num_answers: int
    max_new_tokens: int
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    seed: int
    prompt_tokens: int
    total_generated_tokens: int
    generation_seconds: float
    generated_at: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="User prompt for which all answers are sampled.",
    )
    parser.add_argument(
        "--num-answers",
        type=int,
        default=DEFAULT_NUM_ANSWERS,
        help=(
            "Number of answers to sample in the batch "
            f"(default: {DEFAULT_NUM_ANSWERS})."
        ),
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=32_768,
        help=(
            "Maximum generated tokens per answer, including reasoning "
            "(default: 32768)."
        ),
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
        help="Random seed for reproducible sampling (default: 0).",
    )
    parser.add_argument(
        "--tensor-parallel-size",
        type=int,
        default=1,
        help="Number of GPUs across which vLLM shards the model (default: 1).",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=0.9,
        help="Fraction of each GPU's memory vLLM may use (default: 0.9).",
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
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=None,
        help=(
            "Output path without an extension. The script adds .csv and .md; "
            "the default is a timestamped name under outputs/."
        ),
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.prompt.strip():
        raise SystemExit("--prompt must not be empty.")
    if args.num_answers <= 0:
        raise SystemExit("--num-answers must be greater than 0.")
    if args.max_new_tokens <= 0:
        raise SystemExit("--max-new-tokens must be greater than 0.")
    if args.temperature <= 0:
        raise SystemExit("--temperature must be greater than 0 for sampling.")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be greater than 0.")
    if not 0 <= args.min_p <= 1:
        raise SystemExit("--min-p must be in [0, 1].")
    if args.tensor_parallel_size <= 0:
        raise SystemExit("--tensor-parallel-size must be greater than 0.")
    if not 0 < args.gpu_memory_utilization <= 1:
        raise SystemExit("--gpu-memory-utilization must be in (0, 1].")
    if not args.reasoning_end_marker:
        raise SystemExit("--reasoning-end-marker must not be empty.")


def configure_cache(cache_dir: Path | None) -> Path:
    """Choose a persistent Hugging Face cache before importing vLLM."""
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


def marker_token_ids(tokenizer: Any, marker: str) -> list[int]:
    marker_id = tokenizer.convert_tokens_to_ids(marker)
    unknown_id = getattr(tokenizer, "unk_token_id", None)
    if marker_id is not None and marker_id != unknown_id:
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
    answer = tokenizer.decode(
        output_ids[answer_start:], skip_special_tokens=True
    ).strip()
    return reasoning, answer, True


def output_paths(requested_prefix: Path | None) -> tuple[Path, Path]:
    """Return the CSV and Markdown paths without overwriting existing output."""
    if requested_prefix is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        prefix = Path.cwd() / "outputs" / f"qwen_reasoning_batch_{timestamp}"
    else:
        prefix = requested_prefix.expanduser()
        if not prefix.is_absolute():
            prefix = Path.cwd() / prefix

    csv_path = Path(f"{prefix}.csv").resolve()
    markdown_path = Path(f"{prefix}.md").resolve()
    existing = [path for path in (csv_path, markdown_path) if path.exists()]
    if existing:
        paths = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing output: {paths}")
    return csv_path, markdown_path


def completion_to_answer(
    completion: Any,
    *,
    answer_number: int,
    prompt: str,
    tokenizer: Any,
    reasoning_end_marker: str,
) -> BatchAnswer:
    """Convert one vLLM completion into a serializable result."""
    token_ids = [int(token_id) for token_id in completion.token_ids]
    reasoning, answer, complete = split_reasoning_output(
        token_ids, tokenizer, reasoning_end_marker
    )
    raw_response = tokenizer.decode(token_ids, skip_special_tokens=False).strip()
    finish_reason = getattr(completion, "finish_reason", None)
    stop_reason = getattr(completion, "stop_reason", None)
    return BatchAnswer(
        answer_number=answer_number,
        prompt=prompt,
        reasoning=reasoning,
        answer=answer,
        reasoning_complete=complete,
        raw_response=raw_response,
        finish_reason="" if finish_reason is None else str(finish_reason),
        stop_reason="" if stop_reason is None else str(stop_reason),
        generated_tokens=len(token_ids),
    )


def _fenced_text(text: str) -> str:
    """Render arbitrary model text safely inside a Markdown code fence."""
    longest_run = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}\n{text}\n{fence}"


def _csv_text(answers: Sequence[BatchAnswer], metadata: RunMetadata) -> str:
    destination = StringIO(newline="")
    fieldnames = [
        "answer_number",
        "model",
        "prompt",
        "reasoning",
        "answer",
        "reasoning_complete",
        "raw_response",
        "finish_reason",
        "stop_reason",
        "generated_tokens",
    ]
    writer = csv.DictWriter(destination, fieldnames=fieldnames)
    writer.writeheader()
    for result in answers:
        writer.writerow(
            {
                "answer_number": result.answer_number,
                "model": metadata.model,
                "prompt": result.prompt,
                "reasoning": result.reasoning,
                "answer": result.answer,
                "reasoning_complete": result.reasoning_complete,
                "raw_response": result.raw_response,
                "finish_reason": result.finish_reason,
                "stop_reason": result.stop_reason,
                "generated_tokens": result.generated_tokens,
            }
        )
    return destination.getvalue()


def _markdown_text(answers: Sequence[BatchAnswer], metadata: RunMetadata) -> str:
    destination = StringIO()
    throughput = (
        metadata.total_generated_tokens / metadata.generation_seconds
        if metadata.generation_seconds > 0
        else 0.0
    )
    print("# Qwen reasoning batch", file=destination)
    print(file=destination)
    print(f"- Generated: {metadata.generated_at}", file=destination)
    print(f"- Model: `{metadata.model}`", file=destination)
    print(f"- Answers: {metadata.num_answers}", file=destination)
    print(f"- Prompt tokens: {metadata.prompt_tokens}", file=destination)
    print(f"- Generated tokens: {metadata.total_generated_tokens}", file=destination)
    print(f"- Generation time: {metadata.generation_seconds:.2f} s", file=destination)
    print(f"- Throughput: {throughput:.2f} tokens/s", file=destination)
    print(f"- Temperature: {metadata.temperature}", file=destination)
    print(
        "- Top-p / top-k / min-p: "
        f"{metadata.top_p} / {metadata.top_k} / {metadata.min_p}",
        file=destination,
    )
    print(f"- Seed: {metadata.seed}", file=destination)
    print(
        f"- Maximum new tokens per answer: {metadata.max_new_tokens}",
        file=destination,
    )
    print("\n## Prompt\n", file=destination)
    print(_fenced_text(metadata.prompt), file=destination)

    for result in answers:
        print(f"\n---\n\n## Answer {result.answer_number}\n", file=destination)
        print(
            f"*{result.generated_tokens} generated tokens; finish reason: "
            f"`{result.finish_reason or 'unknown'}`.*",
            file=destination,
        )
        print("\n### Final answer\n", file=destination)
        if result.reasoning_complete:
            final_text = result.answer or "[The model emitted no final answer.]"
            print(_fenced_text(final_text), file=destination)
        else:
            print(
                "The reasoning marker was not found, so the final answer could not "
                "be separated. The complete model output follows:\n",
                file=destination,
            )
            print(_fenced_text(result.raw_response), file=destination)

        print("\n<details>", file=destination)
        print("<summary>Model-emitted reasoning</summary>\n", file=destination)
        print(
            _fenced_text(result.reasoning or "[No reasoning text was generated.]"),
            file=destination,
        )
        print("\n</details>", file=destination)

    return destination.getvalue()


def write_outputs(
    answers: Sequence[BatchAnswer],
    metadata: RunMetadata,
    csv_path: Path,
    markdown_path: Path,
) -> None:
    """Write exactly one CSV and one Markdown file for a completed batch."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    csv_contents = _csv_text(answers, metadata)
    markdown_contents = _markdown_text(answers, metadata)

    # Exclusive creation prevents an explicit output prefix from silently
    # replacing a previous experiment.
    with csv_path.open("x", encoding="utf-8", newline="") as csv_file:
        csv_file.write(csv_contents)
    try:
        with markdown_path.open("x", encoding="utf-8") as markdown_file:
            markdown_file.write(markdown_contents)
    except Exception:
        # Keep the pair invariant if the second exclusive create unexpectedly
        # fails after the preflight existence check.
        csv_path.unlink()
        raise


def main() -> int:
    args = parse_args()
    validate_args(args)
    try:
        csv_path, markdown_path = output_paths(args.output_prefix)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    cache_root = configure_cache(args.cache_dir)
    try:
        from vllm import LLM, SamplingParams
    except ImportError as exc:
        print(
            "Missing dependency: vLLM. Install it in the RunPod environment with "
            "`python3 -m pip install vllm`.",
            file=sys.stderr,
        )
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

    print("=== Environment ===")
    print(f"Model:          {args.model}")
    print(f"HF cache:       {cache_root}")
    print(f"Answers:        {args.num_answers}")
    print(f"Tensor parallel: {args.tensor_parallel_size} GPU(s)")

    try:
        llm = LLM(
            model=args.model,
            tensor_parallel_size=args.tensor_parallel_size,
            gpu_memory_utilization=args.gpu_memory_utilization,
            seed=args.seed,
        )
    except Exception as exc:
        print(f"Failed to initialize vLLM: {exc}", file=sys.stderr)
        return 2

    tokenizer = llm.get_tokenizer()
    rendered_prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": args.prompt}],
        add_generation_prompt=True,
        tokenize=False,
    )
    sampling_params = SamplingParams(
        n=args.num_answers,
        max_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        min_p=args.min_p,
        seed=args.seed,
    )

    print("\nGenerating all answers in one vLLM request...")
    started = time.perf_counter()
    try:
        request_outputs = llm.generate(
            [rendered_prompt], sampling_params, use_tqdm=True
        )
    except Exception as exc:
        print(f"vLLM generation failed: {exc}", file=sys.stderr)
        return 3
    generation_seconds = time.perf_counter() - started

    if len(request_outputs) != 1:
        print(
            f"Expected one request output, received {len(request_outputs)}.",
            file=sys.stderr,
        )
        return 3

    request_output = request_outputs[0]
    completions = sorted(request_output.outputs, key=lambda output: output.index)
    if len(completions) != args.num_answers:
        print(
            f"Expected {args.num_answers} answers, received {len(completions)}.",
            file=sys.stderr,
        )
        return 3

    answers = [
        completion_to_answer(
            completion,
            answer_number=index,
            prompt=args.prompt,
            tokenizer=tokenizer,
            reasoning_end_marker=args.reasoning_end_marker,
        )
        for index, completion in enumerate(completions, start=1)
    ]
    prompt_token_ids = getattr(request_output, "prompt_token_ids", None) or []
    total_generated_tokens = sum(result.generated_tokens for result in answers)
    metadata = RunMetadata(
        model=args.model,
        prompt=args.prompt,
        num_answers=len(answers),
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        min_p=args.min_p,
        seed=args.seed,
        prompt_tokens=len(prompt_token_ids),
        total_generated_tokens=total_generated_tokens,
        generation_seconds=generation_seconds,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )

    try:
        write_outputs(answers, metadata, csv_path, markdown_path)
    except OSError as exc:
        print(f"Could not write output files: {exc}", file=sys.stderr)
        return 5

    throughput = (
        total_generated_tokens / generation_seconds if generation_seconds > 0 else 0.0
    )
    complete_count = sum(result.reasoning_complete for result in answers)
    print("\n=== Batch complete ===")
    print(f"Answers:          {len(answers)}")
    print(f"Complete splits:  {complete_count}/{len(answers)}")
    print(f"Generated tokens: {total_generated_tokens}")
    print(f"Generation time:  {generation_seconds:.2f} s")
    print(f"Throughput:       {throughput:.2f} tokens/s")
    print(f"CSV:              {csv_path}")
    print(f"Markdown:         {markdown_path}")

    if complete_count != len(answers):
        print(
            "Warning: at least one answer did not contain the reasoning end marker. "
            "Its complete raw output was still saved.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
