#!/usr/bin/env python3
"""Sample every criticism-baseline prompt efficiently with vLLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from batch_qwen_reasoning_vllm import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_END_MARKER,
    configure_cache,
    split_reasoning_output,
)


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS = ROOT_DIR / "prompts" / "criticism_baseline.json"


@dataclass(frozen=True)
class PromptRequest:
    """One prompt-condition pair for which vLLM will sample many completions."""

    prompt_id: str
    level: int
    level_label: str
    title: str
    condition: str
    prompt: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--condition",
        choices=("natural", "criticism_eliciting", "both"),
        default="natural",
        help="Prompt ending(s) to run (default: natural).",
    )
    parser.add_argument(
        "--samples-per-prompt",
        type=int,
        default=16,
        help="Stochastic completions per prompt and condition (default: 16).",
    )
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--min-p", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tensor-parallel-size", type=int, default=1)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--system-prompt",
        help="Override the system prompt stored in the prompt file.",
    )
    parser.add_argument(
        "--prompt-id",
        action="append",
        dest="prompt_ids",
        help="Only run this prompt ID; repeat the flag to select several.",
    )
    parser.add_argument(
        "--reasoning-end-marker",
        default=DEFAULT_REASONING_END_MARKER,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview the experiment without loading vLLM.",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    for name in ("samples_per_prompt", "max_new_tokens", "tensor_parallel_size"):
        if getattr(args, name) <= 0:
            option = "--" + name.replace("_", "-")
            raise SystemExit(f"{option} must be greater than 0.")
    if args.temperature <= 0:
        raise SystemExit("--temperature must be greater than 0 for sampling.")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be greater than 0.")
    if not 0 <= args.min_p <= 1:
        raise SystemExit("--min-p must be in [0, 1].")
    if not 0 < args.gpu_memory_utilization <= 1:
        raise SystemExit("--gpu-memory-utilization must be in (0, 1].")
    if not args.reasoning_end_marker:
        raise SystemExit("--reasoning-end-marker must not be empty.")


def load_prompt_set(path: Path) -> dict[str, Any]:
    """Load and minimally validate the structured prompt-set JSON file."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Prompt file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Prompt file is not valid JSON: {exc}") from exc

    if not isinstance(data.get("conditions"), dict) or not data["conditions"]:
        raise ValueError("Prompt file must contain a non-empty 'conditions' object.")
    if not isinstance(data.get("prompts"), list) or not data["prompts"]:
        raise ValueError("Prompt file must contain a non-empty 'prompts' list.")

    required = {"id", "level", "level_label", "title", "proposal"}
    seen_ids: set[str] = set()
    for index, prompt in enumerate(data["prompts"]):
        if not isinstance(prompt, dict):
            raise ValueError(f"Prompt {index} must be an object.")
        missing = required - prompt.keys()
        if missing:
            raise ValueError(f"Prompt {index} is missing: {', '.join(sorted(missing))}")
        if prompt["id"] in seen_ids:
            raise ValueError(f"Duplicate prompt ID: {prompt['id']}")
        seen_ids.add(prompt["id"])
    return data


def selected_conditions(condition: str) -> list[str]:
    return ["natural", "criticism_eliciting"] if condition == "both" else [condition]


def build_requests(
    prompt_set: dict[str, Any],
    *,
    condition: str,
    prompt_ids: set[str] | None = None,
) -> list[PromptRequest]:
    """Create one vLLM request for each selected prompt-condition pair."""

    prompts = prompt_set["prompts"]
    known_ids = {prompt["id"] for prompt in prompts}
    if prompt_ids:
        unknown = prompt_ids - known_ids
        if unknown:
            raise ValueError(f"Unknown prompt ID(s): {', '.join(sorted(unknown))}")
        prompts = [prompt for prompt in prompts if prompt["id"] in prompt_ids]

    requests: list[PromptRequest] = []
    for prompt in prompts:
        for condition_name in selected_conditions(condition):
            try:
                ending = prompt_set["conditions"][condition_name]
            except KeyError as exc:
                raise ValueError(
                    f"Prompt file has no '{condition_name}' condition."
                ) from exc
            requests.append(
                PromptRequest(
                    prompt_id=prompt["id"],
                    level=prompt["level"],
                    level_label=prompt["level_label"],
                    title=prompt["title"],
                    condition=condition_name,
                    prompt=f"{prompt['proposal'].strip()}\n\n{ending.strip()}",
                )
            )
    return requests


def output_path(requested: Path | None) -> Path:
    """Choose a timestamped default and refuse to overwrite an existing run."""

    if requested is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = ROOT_DIR / "outputs" / f"criticism_baseline_{timestamp}.jsonl"
    else:
        path = requested.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    return path


def parse_completion(
    completion: Any,
    *,
    tokenizer: Any,
    reasoning_end_marker: str,
) -> dict[str, Any]:
    """Decode one completion, supporting both thinking and instruct models."""

    token_ids = [int(token_id) for token_id in completion.token_ids]
    reasoning, answer, reasoning_complete = split_reasoning_output(
        token_ids, tokenizer, reasoning_end_marker
    )
    clean_output = tokenizer.decode(token_ids, skip_special_tokens=True).strip()
    raw_response = tokenizer.decode(token_ids, skip_special_tokens=False).strip()
    if reasoning_complete:
        response = answer
        reasoning_text: str | None = reasoning
    else:
        response = clean_output
        reasoning_text = None

    finish_reason = getattr(completion, "finish_reason", None)
    stop_reason = getattr(completion, "stop_reason", None)
    return {
        "response": response,
        "reasoning": reasoning_text,
        "reasoning_complete": reasoning_complete,
        "raw_response": raw_response,
        "finish_reason": "" if finish_reason is None else str(finish_reason),
        "stop_reason": "" if stop_reason is None else str(stop_reason),
        "generated_tokens": len(token_ids),
    }


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    try:
        prompt_set = load_prompt_set(args.prompts)
        requests = build_requests(
            prompt_set,
            condition=args.condition,
            prompt_ids=set(args.prompt_ids) if args.prompt_ids else None,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_samples = len(requests) * args.samples_per_prompt
    system_prompt = args.system_prompt or prompt_set.get(
        "system_prompt", "You are a helpful assistant."
    )
    print(
        f"Prepared {len(requests)} prompt-condition requests and "
        f"{total_samples} total samples."
    )
    if args.dry_run:
        print("\nFirst user prompt:\n")
        print(requests[0].prompt)
        return 0

    try:
        destination = output_path(args.output)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    configure_cache(args.cache_dir)
    try:
        from vllm import LLM, SamplingParams
    except ImportError as exc:
        print(
            "Missing dependency: vLLM. Install it with "
            "`python3 -m pip install vllm`.",
            file=sys.stderr,
        )
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

    print(f"Loading {args.model}...")
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
    rendered_prompts = [
        tokenizer.apply_chat_template(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ],
            add_generation_prompt=True,
            tokenize=False,
        )
        for request in requests
    ]
    sampling_params = SamplingParams(
        n=args.samples_per_prompt,
        max_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        min_p=args.min_p,
        seed=args.seed,
    )

    print("Generating all prompts in one dynamically batched vLLM call...")
    started = time.perf_counter()
    try:
        request_outputs = llm.generate(
            rendered_prompts, sampling_params, use_tqdm=True
        )
    except Exception as exc:
        print(f"vLLM generation failed: {exc}", file=sys.stderr)
        return 3
    elapsed = time.perf_counter() - started

    if len(request_outputs) != len(requests):
        print(
            f"Expected {len(requests)} request outputs; got {len(request_outputs)}.",
            file=sys.stderr,
        )
        return 3

    generation_config = {
        "model": args.model,
        "system_prompt": system_prompt,
        "samples_per_prompt": args.samples_per_prompt,
        "max_new_tokens": args.max_new_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "min_p": args.min_p,
        "seed": args.seed,
        "tensor_parallel_size": args.tensor_parallel_size,
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "prompt_file": str(args.prompts.resolve()),
        "prompt_file_sha256": hashlib.sha256(args.prompts.read_bytes()).hexdigest(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    records_written = 0
    total_generated_tokens = 0
    incomplete_reasoning = 0
    try:
        with destination.open("x", encoding="utf-8") as output_file:
            for request, request_output in zip(requests, request_outputs):
                completions = sorted(
                    request_output.outputs, key=lambda output: output.index
                )
                if len(completions) != args.samples_per_prompt:
                    raise RuntimeError(
                        f"{request.prompt_id}/{request.condition}: expected "
                        f"{args.samples_per_prompt} completions, got {len(completions)}"
                    )
                for sample_number, completion in enumerate(completions, start=1):
                    parsed = parse_completion(
                        completion,
                        tokenizer=tokenizer,
                        reasoning_end_marker=args.reasoning_end_marker,
                    )
                    record = {
                        "sample_id": (
                            f"{request.prompt_id}.{request.condition}."
                            f"s{sample_number:02d}"
                        ),
                        **asdict(request),
                        "sample_number": sample_number,
                        **parsed,
                        "generation_config": generation_config,
                    }
                    output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    records_written += 1
                    total_generated_tokens += parsed["generated_tokens"]
                    incomplete_reasoning += not parsed["reasoning_complete"]
    except (OSError, RuntimeError) as exc:
        print(f"Could not write complete results: {exc}", file=sys.stderr)
        return 4

    throughput = total_generated_tokens / elapsed if elapsed > 0 else 0.0
    print("\n=== Criticism baseline complete ===")
    print(f"Samples:          {records_written}")
    print(f"Generated tokens: {total_generated_tokens}")
    print(f"Generation time:  {elapsed:.2f} s")
    print(f"Throughput:       {throughput:.2f} tokens/s")
    print(f"Output:           {destination}")
    if incomplete_reasoning:
        print(
            f"Note: {incomplete_reasoning} outputs had no reasoning marker; they were "
            "saved as ordinary non-thinking responses.",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
