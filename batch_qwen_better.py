#!/usr/bin/env python3
"""Sample every criticism-baseline prompt in manual Transformers mini-batches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from hello_qwen_reasoning import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_END_MARKER,
    choose_dtype,
    configure_cache,
    dtype_load_kwargs,
    input_device_for,
    split_reasoning_output,
)


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS = ROOT_DIR / "prompts" / "criticism_baseline_better.json"


@dataclass(frozen=True)
class PromptRequest:
    """One prompt-condition pair for which the model will produce samples."""

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
        help="Stochastic samples per prompt and condition (default: 16).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Number of samples generated simultaneously (default: 8).",
    )
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=0)
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
        help="Validate and preview the experiment without loading the model.",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    for name in ("samples_per_prompt", "batch_size", "max_new_tokens"):
        if getattr(args, name) <= 0:
            option = "--" + name.replace("_", "-")
            raise SystemExit(f"{option} must be greater than 0.")
    if args.temperature <= 0:
        raise SystemExit("--temperature must be greater than 0 for sampling.")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--top-p must be in (0, 1].")
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
    """Create one request for each selected prompt-condition pair."""

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


def batch_ranges(total: int, batch_size: int) -> Iterator[tuple[int, int]]:
    """Yield half-open ranges for manual mini-batching."""

    for start in range(0, total, batch_size):
        yield start, min(start + batch_size, total)


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


def load_runtime(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Load the same PyTorch/Transformers stack as batch-inference.py."""

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
    tokenizer: Any,
    *,
    request: PromptRequest,
    system_prompt: str,
    batch_size: int,
    input_device: Any,
) -> Any:
    """Render and tokenize one repeated-prompt mini-batch."""

    messages = [
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.prompt},
        ]
        for _ in range(batch_size)
    ]
    texts = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
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
    """Remove batch padding after the first generated EOS, retaining the EOS."""

    result = [int(token_id) for token_id in token_ids]
    for index, token_id in enumerate(result):
        if token_id in eos_ids:
            return result[: index + 1]
    return result


def parse_tokens(
    token_ids: Sequence[int],
    *,
    tokenizer: Any,
    reasoning_end_marker: str,
    eos_ids: set[int],
) -> dict[str, Any]:
    """Decode one generated sequence from a thinking or instruct checkpoint."""

    parsed = split_reasoning_output(token_ids, tokenizer, reasoning_end_marker)
    reasoning, answer, reasoning_complete = parsed[:3]
    reasoning_tokens = int(parsed[3]) if len(parsed) >= 5 else 0
    answer_tokens = int(parsed[4]) if len(parsed) >= 5 else len(token_ids)
    clean_output = tokenizer.decode(token_ids, skip_special_tokens=True).strip()
    raw_response = tokenizer.decode(token_ids, skip_special_tokens=False).strip()
    if reasoning_complete:
        response = answer
        reasoning_text: str | None = reasoning
    else:
        response = clean_output
        reasoning_text = None
        reasoning_tokens = 0
        answer_tokens = len(token_ids)

    return {
        "response": response,
        "reasoning": reasoning_text,
        "reasoning_complete": reasoning_complete,
        "raw_response": raw_response,
        "finish_reason": "stop" if token_ids and token_ids[-1] in eos_ids else "length",
        "reasoning_tokens": reasoning_tokens,
        "response_tokens": answer_tokens,
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
        f"{total_samples} total samples (manual batch size {args.batch_size})."
    )
    if args.dry_run:
        print("\nFirst user prompt:\n")
        print(requests[0].prompt)
        return 0

    try:
        destination = output_path(args.output)
        torch, tokenizer, model = load_runtime(args)
    except (FileExistsError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    generation_config = {
        "backend": "pytorch_transformers",
        "model": args.model,
        "system_prompt": system_prompt,
        "samples_per_prompt": args.samples_per_prompt,
        "batch_size": args.batch_size,
        "max_new_tokens": args.max_new_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "seed": args.seed,
        "prompt_file": str(args.prompts.resolve()),
        "prompt_file_sha256": hashlib.sha256(args.prompts.read_bytes()).hexdigest(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    generation_options = {
        "do_sample": True,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_new_tokens": args.max_new_tokens,
        "use_cache": True,
        "pad_token_id": tokenizer.pad_token_id,
    }
    input_device = input_device_for(model)
    endings = eos_token_ids(model, tokenizer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = 0
    total_generated_tokens = 0
    incomplete_reasoning = 0

    torch.cuda.synchronize()
    started = time.perf_counter()
    try:
        with destination.open("x", encoding="utf-8") as output_file:
            for request in requests:
                for start, end in batch_ranges(
                    args.samples_per_prompt, args.batch_size
                ):
                    current_size = end - start
                    print(
                        f"{request.prompt_id}/{request.condition}: samples "
                        f"{start + 1}-{end}/{args.samples_per_prompt} "
                        f"({completed}/{total_samples} complete)"
                    )
                    inputs = prepare_inputs(
                        tokenizer,
                        request=request,
                        system_prompt=system_prompt,
                        batch_size=current_size,
                        input_device=input_device,
                    )
                    input_width = int(inputs["input_ids"].shape[1])
                    with torch.inference_mode():
                        sequences = model.generate(**inputs, **generation_options)
                    generated_rows = sequences[:, input_width:].detach().cpu().tolist()

                    for offset, row in enumerate(generated_rows, start=1):
                        sample_number = start + offset
                        token_ids = trim_generated_tokens(row, endings)
                        parsed = parse_tokens(
                            token_ids,
                            tokenizer=tokenizer,
                            reasoning_end_marker=args.reasoning_end_marker,
                            eos_ids=endings,
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
                        output_file.flush()
                        completed += 1
                        total_generated_tokens += parsed["generated_tokens"]
                        incomplete_reasoning += not parsed["reasoning_complete"]
                    del inputs, sequences, generated_rows
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        print(
            "CUDA ran out of memory. Retry with a smaller --batch-size or fewer "
            "--max-new-tokens. The JSONL file contains the completed samples.",
            file=sys.stderr,
        )
        return 3

    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    throughput = total_generated_tokens / elapsed if elapsed > 0 else 0.0
    print("\n=== Criticism baseline complete ===")
    print(f"Samples:          {completed}")
    print(f"Batch size:       {args.batch_size}")
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
