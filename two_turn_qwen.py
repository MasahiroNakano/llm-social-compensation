#!/usr/bin/env python3
"""Generate one Qwen response after reconstructing a saved first chat turn."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from batch_qwen import (
    DEFAULT_PROMPTS,
    build_requests,
    eos_token_ids,
    load_prompt_set,
    load_runtime,
    parse_tokens,
    trim_generated_tokens,
)
from hello_qwen_reasoning import DEFAULT_REASONING_END_MARKER, input_device_for


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_JSONL = (
    ROOT_DIR
    / "outputs"
    / "criticism_baseline_2026-09-04_14-42-59_783609.jsonl"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-jsonl", type=Path, default=DEFAULT_SOURCE_JSONL)
    parser.add_argument(
        "--source-row",
        type=int,
        default=20,
        help="1-based nonblank JSONL row used as the first turn (default: 20).",
    )
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--followup-prompt-id", default="L5_17")
    parser.add_argument(
        "--followup-condition",
        choices=("natural", "criticism_eliciting"),
        help="Defaults to the condition saved in the source row.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--model",
        help="Defaults to the model recorded in the source row.",
    )
    parser.add_argument(
        "--system-prompt",
        help="Defaults to the system prompt recorded in the source row.",
    )
    parser.add_argument("--max-new-tokens", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--reasoning-end-marker",
        default=DEFAULT_REASONING_END_MARKER,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the reconstructed messages without loading Qwen.",
    )
    return parser.parse_args(argv)


def load_jsonl_row(path: Path, row_number: int) -> dict[str, Any]:
    """Load a 1-based nonblank row from a JSONL file."""

    if row_number <= 0:
        raise ValueError("--source-row must be greater than 0.")
    try:
        with path.open(encoding="utf-8") as input_file:
            current_row = 0
            for physical_line, line in enumerate(input_file, start=1):
                if not line.strip():
                    continue
                current_row += 1
                if current_row != row_number:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in {path} on physical line {physical_line}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"Source row {row_number} must be a JSON object.")
                return record
    except FileNotFoundError as exc:
        raise ValueError(f"Source JSONL file does not exist: {path}") from exc
    raise ValueError(f"Source JSONL has fewer than {row_number} nonblank rows: {path}")


def validate_source_record(record: dict[str, Any]) -> None:
    for field in ("prompt", "response"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Source row must contain a non-empty {field!r} string.")
    config = record.get("generation_config")
    if not isinstance(config, dict):
        raise ValueError("Source row must contain a 'generation_config' object.")


def source_setting(
    args: argparse.Namespace,
    source_config: dict[str, Any],
    name: str,
    fallback: Any,
) -> Any:
    override = getattr(args, name)
    return source_config.get(name, fallback) if override is None else override


def build_messages(
    *,
    source_record: dict[str, Any],
    followup_prompt: str,
    system_prompt: str,
) -> list[dict[str, str]]:
    """Construct system, saved user/assistant, and new user messages."""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": source_record["prompt"].strip()},
        {"role": "assistant", "content": source_record["response"].strip()},
        {"role": "user", "content": followup_prompt.strip()},
    ]


def output_path(requested: Path | None) -> Path:
    if requested is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = ROOT_DIR / "outputs" / f"criticism_two_turn_smoke_{timestamp}.jsonl"
    else:
        path = requested.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    return path


def validate_generation_settings(
    *,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    reasoning_end_marker: str,
) -> None:
    if max_new_tokens <= 0:
        raise ValueError("--max-new-tokens must be greater than 0.")
    if temperature <= 0:
        raise ValueError("--temperature must be greater than 0 for sampling.")
    if not 0 < top_p <= 1:
        raise ValueError("--top-p must be in (0, 1].")
    if not reasoning_end_marker:
        raise ValueError("--reasoning-end-marker must not be empty.")


def run(args: argparse.Namespace) -> int:
    try:
        source_path = args.source_jsonl.expanduser().resolve()
        prompts_path = args.prompts.expanduser().resolve()
        source_record = load_jsonl_row(source_path, args.source_row)
        validate_source_record(source_record)
        source_config = source_record["generation_config"]
        prompt_set = load_prompt_set(prompts_path)
        followup_condition = args.followup_condition or source_record.get("condition")
        if followup_condition not in {"natural", "criticism_eliciting"}:
            raise ValueError(
                "Could not infer a valid follow-up condition from the source row; "
                "pass --followup-condition explicitly."
            )
        requests = build_requests(
            prompt_set,
            condition=followup_condition,
            prompt_ids={args.followup_prompt_id},
        )
        followup = requests[0]
        model_name = source_setting(
            args, source_config, "model", "Qwen/Qwen3.5-4B"
        )
        system_prompt = source_setting(
            args,
            source_config,
            "system_prompt",
            prompt_set.get("system_prompt", "You are a helpful assistant."),
        )
        max_new_tokens = int(
            source_setting(args, source_config, "max_new_tokens", 4096)
        )
        temperature = float(source_setting(args, source_config, "temperature", 0.7))
        top_p = float(source_setting(args, source_config, "top_p", 0.95))
        seed = int(source_setting(args, source_config, "seed", 0))
        validate_generation_settings(
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_end_marker=args.reasoning_end_marker,
        )
        messages = build_messages(
            source_record=source_record,
            followup_prompt=followup.prompt,
            system_prompt=system_prompt,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Prepared source row {args.source_row} ({source_record.get('sample_id')}) "
        f"followed by {followup.prompt_id}/{followup.condition}."
    )
    if args.dry_run:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return 0

    try:
        destination = output_path(args.output)
    except (FileExistsError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    runtime_args = argparse.Namespace(
        cache_dir=args.cache_dir,
        model=model_name,
        seed=seed,
    )
    try:
        torch, tokenizer, model = load_runtime(runtime_args)
        rendered = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = tokenizer(
            rendered,
            return_tensors="pt",
            add_special_tokens=False,
        ).to(input_device_for(model))
        prompt_tokens = int(inputs["input_ids"].shape[1])
        endings = eos_token_ids(model, tokenizer)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            sequences = model.generate(
                **inputs,
                do_sample=True,
                temperature=temperature,
                top_p=top_p,
                max_new_tokens=max_new_tokens,
                use_cache=True,
                pad_token_id=tokenizer.pad_token_id,
            )
        torch.cuda.synchronize()
        generation_seconds = time.perf_counter() - started
        token_ids = trim_generated_tokens(
            sequences[0, prompt_tokens:].detach().cpu().tolist(), endings
        )
        parsed = parse_tokens(
            token_ids,
            tokenizer=tokenizer,
            reasoning_end_marker=args.reasoning_end_marker,
            eos_ids=endings,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        conversation = [
            *messages,
            {"role": "assistant", "content": parsed["response"]},
        ]
        record = {
            "sample_id": (
                f"{source_record.get('sample_id', f'row{args.source_row}')}"
                f"__to__{followup.prompt_id}.{followup.condition}.s01"
            ),
            "source": {
                "jsonl": str(source_path),
                "jsonl_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "row": args.source_row,
                "sample_id": source_record.get("sample_id"),
                "prompt_id": source_record.get("prompt_id"),
                "condition": source_record.get("condition"),
                "sample_number": source_record.get("sample_number"),
            },
            "followup": {
                "prompt_file": str(prompts_path),
                "prompt_file_sha256": hashlib.sha256(prompts_path.read_bytes()).hexdigest(),
                "prompt_id": followup.prompt_id,
                "level": followup.level,
                "level_label": followup.level_label,
                "title": followup.title,
                "condition": followup.condition,
                "prompt": followup.prompt,
            },
            "messages": messages,
            "conversation": conversation,
            **parsed,
            "prompt_tokens": prompt_tokens,
            "generation_seconds": generation_seconds,
            "generation_config": {
                "backend": "pytorch_transformers",
                "model": model_name,
                "system_prompt": system_prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "seed": seed,
                "reasoning_end_marker": args.reasoning_end_marker,
                "generated_at": generated_at,
            },
        }
        with destination.open("x", encoding="utf-8") as output_file:
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (FileExistsError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Generated {parsed['generated_tokens']} tokens in {generation_seconds:.2f}s.")
    print(f"Saved one JSONL record to: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
