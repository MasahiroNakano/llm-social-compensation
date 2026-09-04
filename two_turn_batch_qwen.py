#!/usr/bin/env python3
"""Sample fixed follow-ups after a set of saved first-turn Qwen responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from batch_qwen import (
    DEFAULT_PROMPTS,
    batch_ranges,
    build_requests,
    eos_token_ids,
    load_prompt_set,
    load_runtime,
    parse_tokens,
    trim_generated_tokens,
)
from hello_qwen_reasoning import DEFAULT_REASONING_END_MARKER, input_device_for
from two_turn_qwen import (
    build_messages,
    validate_generation_settings,
    validate_source_record,
)


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_JSONL = (
    ROOT_DIR
    / "outputs"
    / "criticism_baseline_2026-09-04_14-42-59_783609.jsonl"
)


@dataclass(frozen=True)
class SourceTurn:
    """A selected saved response and its 1-based nonblank JSONL row."""

    row_number: int
    record: dict[str, Any]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-jsonl", type=Path, default=DEFAULT_SOURCE_JSONL)
    parser.add_argument("--source-prompt-id", default="L3_11")
    parser.add_argument(
        "--source-condition",
        choices=("natural", "criticism_eliciting"),
        default="natural",
    )
    parser.add_argument(
        "--expected-source-count",
        type=int,
        default=16,
        help="Fail unless this many first-turn responses are found (default: 16).",
    )
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--followup-prompt-id", default="L3_09")
    parser.add_argument(
        "--followup-condition",
        choices=("natural", "criticism_eliciting"),
        default="natural",
    )
    parser.add_argument(
        "--samples-per-source",
        type=int,
        default=8,
        help="Second-turn samples per saved first-turn response (default: 8).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Second-turn samples generated simultaneously (default: 8).",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", help="Defaults to the source records' model.")
    parser.add_argument(
        "--system-prompt",
        help="Defaults to the source records' system prompt.",
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
        "--resume",
        action="store_true",
        help="Validate an existing --output file and append missing samples.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview the experiment without loading Qwen.",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    for name in ("expected_source_count", "samples_per_source", "batch_size"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be greater than 0.")


def load_source_turns(
    path: Path,
    *,
    prompt_id: str,
    condition: str,
    expected_count: int,
) -> list[SourceTurn]:
    """Select and validate saved first turns while retaining source row numbers."""

    selected: list[SourceTurn] = []
    sample_ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as input_file:
            nonblank_row = 0
            for physical_line, line in enumerate(input_file, start=1):
                if not line.strip():
                    continue
                nonblank_row += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in {path} on physical line {physical_line}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"Source row {nonblank_row} must be a JSON object."
                    )
                if record.get("prompt_id") != prompt_id:
                    continue
                if record.get("condition") != condition:
                    continue
                validate_source_record(record)
                sample_id = record.get("sample_id")
                if not isinstance(sample_id, str) or not sample_id:
                    raise ValueError(
                        f"Source row {nonblank_row} has no non-empty sample_id."
                    )
                if sample_id in sample_ids:
                    raise ValueError(f"Duplicate source sample_id: {sample_id!r}.")
                sample_ids.add(sample_id)
                selected.append(SourceTurn(nonblank_row, record))
    except FileNotFoundError as exc:
        raise ValueError(f"Source JSONL file does not exist: {path}") from exc

    if len(selected) != expected_count:
        raise ValueError(
            f"Expected {expected_count} {prompt_id}/{condition} source records, "
            f"but found {len(selected)} in {path}."
        )
    return selected


def common_source_setting(
    source_turns: Sequence[SourceTurn],
    *,
    name: str,
    override: Any,
    fallback: Any,
) -> Any:
    """Use a CLI override or require one consistent saved setting."""

    if override is not None:
        return override
    values = [turn.record["generation_config"].get(name, fallback) for turn in source_turns]
    signatures = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}
    if len(signatures) != 1:
        raise ValueError(
            f"Source records disagree on generation setting {name!r}; "
            f"pass --{name.replace('_', '-')} explicitly."
        )
    return values[0]


def followup_sample_id(
    source_turn: SourceTurn,
    *,
    followup_prompt_id: str,
    followup_condition: str,
    sample_number: int,
) -> str:
    source_sample_id = source_turn.record["sample_id"]
    return (
        f"{source_sample_id}__to__{followup_prompt_id}."
        f"{followup_condition}.s{sample_number:02d}"
    )


def expected_samples(
    source_turns: Sequence[SourceTurn],
    *,
    followup_prompt_id: str,
    followup_condition: str,
    samples_per_source: int,
) -> dict[str, tuple[SourceTurn, int]]:
    return {
        followup_sample_id(
            source_turn,
            followup_prompt_id=followup_prompt_id,
            followup_condition=followup_condition,
            sample_number=sample_number,
        ): (source_turn, sample_number)
        for source_turn in source_turns
        for sample_number in range(1, samples_per_source + 1)
    }


def stable_batch_seed(
    base_seed: int,
    source_sample_id: str,
    followup_prompt_id: str,
    followup_condition: str,
    start: int,
    end: int,
) -> int:
    identity = (
        f"criticism-two-turn-v1:{base_seed}:{source_sample_id}:"
        f"{followup_prompt_id}:{followup_condition}:{start}:{end}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1)


def output_path(requested: Path | None, *, resume: bool) -> Path:
    if resume and requested is None:
        raise ValueError("--resume requires an explicit --output path.")
    if requested is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = ROOT_DIR / "outputs" / f"criticism_two_turn_batch_{timestamp}.jsonl"
    else:
        path = requested.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    path = path.resolve()
    if resume and not path.exists():
        raise ValueError(f"Cannot resume because the output does not exist: {path}")
    if not resume and path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    return path


def load_completed_samples(
    path: Path,
    *,
    expected: dict[str, tuple[SourceTurn, int]],
    required_config: dict[str, Any],
    followup_prompt: str,
) -> set[str]:
    """Validate a partial output and return its completed sample IDs."""

    completed: set[str] = set()
    with path.open(encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Cannot resume: invalid JSON on line {line_number}: {exc}"
                ) from exc
            sample_id = record.get("sample_id")
            if sample_id in completed:
                raise ValueError(f"Cannot resume: duplicate sample_id {sample_id!r}.")
            if sample_id not in expected:
                raise ValueError(
                    f"Cannot resume: unexpected sample_id {sample_id!r} on line "
                    f"{line_number}."
                )
            source_turn, sample_number = expected[sample_id]
            source = record.get("source")
            followup = record.get("followup")
            if not isinstance(source, dict) or not isinstance(followup, dict):
                raise ValueError(
                    f"Cannot resume: line {line_number} lacks source/followup metadata."
                )
            expected_fields = {
                "source.sample_id": (source.get("sample_id"), source_turn.record["sample_id"]),
                "source.row": (source.get("row"), source_turn.row_number),
                "followup.prompt": (followup.get("prompt"), followup_prompt),
                "sample_number": (record.get("sample_number"), sample_number),
            }
            for label, (saved, intended) in expected_fields.items():
                if saved != intended:
                    raise ValueError(
                        f"Cannot resume: {label} differs on line {line_number}."
                    )
            saved_config = record.get("generation_config")
            if not isinstance(saved_config, dict):
                raise ValueError(
                    f"Cannot resume: line {line_number} has no generation_config."
                )
            for key, value in required_config.items():
                if saved_config.get(key) != value:
                    raise ValueError(
                        f"Cannot resume: generation setting {key!r} differs on "
                        f"line {line_number}."
                    )
            completed.add(sample_id)
    return completed


def repeated_messages(
    source_turn: SourceTurn,
    *,
    followup_prompt: str,
    system_prompt: str,
    count: int,
) -> list[list[dict[str, str]]]:
    messages = build_messages(
        source_record=source_turn.record,
        followup_prompt=followup_prompt,
        system_prompt=system_prompt,
    )
    return [messages for _ in range(count)]


def run(args: argparse.Namespace) -> int:
    try:
        validate_args(args)
        source_path = args.source_jsonl.expanduser().resolve()
        prompts_path = args.prompts.expanduser().resolve()
        source_turns = load_source_turns(
            source_path,
            prompt_id=args.source_prompt_id,
            condition=args.source_condition,
            expected_count=args.expected_source_count,
        )
        prompt_set = load_prompt_set(prompts_path)
        followup = build_requests(
            prompt_set,
            condition=args.followup_condition,
            prompt_ids={args.followup_prompt_id},
        )[0]
        model_name = common_source_setting(
            source_turns, name="model", override=args.model,
            fallback="Qwen/Qwen3-4B-Thinking-2507",
        )
        system_prompt = common_source_setting(
            source_turns, name="system_prompt", override=args.system_prompt,
            fallback=prompt_set.get("system_prompt", "You are a helpful assistant."),
        )
        max_new_tokens = int(common_source_setting(
            source_turns, name="max_new_tokens", override=args.max_new_tokens,
            fallback=4096,
        ))
        temperature = float(common_source_setting(
            source_turns, name="temperature", override=args.temperature, fallback=0.7,
        ))
        top_p = float(common_source_setting(
            source_turns, name="top_p", override=args.top_p, fallback=0.95,
        ))
        seed = int(common_source_setting(
            source_turns, name="seed", override=args.seed, fallback=0,
        ))
        if not isinstance(model_name, str) or not model_name:
            raise ValueError("Model name must be a non-empty string.")
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("System prompt must be a non-empty string.")
        validate_generation_settings(
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_end_marker=args.reasoning_end_marker,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_samples = len(source_turns) * args.samples_per_source
    print(
        f"Prepared {len(source_turns)} {args.source_prompt_id}/"
        f"{args.source_condition} first turns × {args.samples_per_source} "
        f"{followup.prompt_id}/{followup.condition} samples = "
        f"{total_samples} generations."
    )
    if args.dry_run:
        print("Source samples:")
        for source_turn in source_turns:
            print(
                f"  row {source_turn.row_number}: "
                f"{source_turn.record['sample_id']}"
            )
        print("\nFixed second-turn prompt:\n")
        print(followup.prompt)
        return 0

    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    prompt_hash = hashlib.sha256(prompts_path.read_bytes()).hexdigest()
    required_config = {
        "backend": "pytorch_transformers",
        "model": model_name,
        "system_prompt": system_prompt,
        "source_prompt_id": args.source_prompt_id,
        "source_condition": args.source_condition,
        "expected_source_count": args.expected_source_count,
        "followup_prompt_id": followup.prompt_id,
        "followup_condition": followup.condition,
        "samples_per_source": args.samples_per_source,
        "batch_size": args.batch_size,
        "max_new_tokens": max_new_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "seed": seed,
        "reasoning_end_marker": args.reasoning_end_marker,
        "source_jsonl_sha256": source_hash,
        "prompt_file_sha256": prompt_hash,
    }
    generation_config = {
        **required_config,
        "source_jsonl": str(source_path),
        "prompt_file": str(prompts_path),
        "seed_strategy": "stable_source_batch_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    intended = expected_samples(
        source_turns,
        followup_prompt_id=followup.prompt_id,
        followup_condition=followup.condition,
        samples_per_source=args.samples_per_source,
    )
    try:
        destination = output_path(args.output, resume=args.resume)
        completed_ids = (
            load_completed_samples(
                destination,
                expected=intended,
                required_config=required_config,
                followup_prompt=followup.prompt,
            )
            if args.resume
            else set()
        )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if len(completed_ids) == total_samples:
        print(f"Nothing to do; all {total_samples} samples exist in {destination}.")
        return 0
    if args.resume:
        print(f"Resume found {len(completed_ids)}/{total_samples} completed samples.")

    runtime_args = argparse.Namespace(cache_dir=args.cache_dir, model=model_name, seed=seed)
    try:
        torch, tokenizer, model = load_runtime(runtime_args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    generation_options = {
        "do_sample": True,
        "temperature": temperature,
        "top_p": top_p,
        "max_new_tokens": max_new_tokens,
        "use_cache": True,
        "pad_token_id": tokenizer.pad_token_id,
    }
    input_device = input_device_for(model)
    endings = eos_token_ids(model, tokenizer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = len(completed_ids)
    records_written = 0
    total_generated_tokens = 0

    torch.cuda.synchronize()
    started = time.perf_counter()
    try:
        with destination.open("a" if args.resume else "x", encoding="utf-8") as output_file:
            for source_index, source_turn in enumerate(source_turns, start=1):
                for start, end in batch_ranges(args.samples_per_source, args.batch_size):
                    batch_ids = {
                        followup_sample_id(
                            source_turn,
                            followup_prompt_id=followup.prompt_id,
                            followup_condition=followup.condition,
                            sample_number=number,
                        )
                        for number in range(start + 1, end + 1)
                    }
                    if batch_ids <= completed_ids:
                        continue
                    batch_seed = stable_batch_seed(
                        seed,
                        source_turn.record["sample_id"],
                        followup.prompt_id,
                        followup.condition,
                        start,
                        end,
                    )
                    torch.manual_seed(batch_seed)
                    torch.cuda.manual_seed_all(batch_seed)
                    current_size = end - start
                    print(
                        f"Source {source_index}/{len(source_turns)} "
                        f"({source_turn.record['sample_id']}): follow-up samples "
                        f"{start + 1}-{end}/{args.samples_per_source} "
                        f"({completed}/{total_samples} complete)"
                    )
                    message_batches = repeated_messages(
                        source_turn,
                        followup_prompt=followup.prompt,
                        system_prompt=system_prompt,
                        count=current_size,
                    )
                    rendered = tokenizer.apply_chat_template(
                        message_batches,
                        tokenize=False,
                        add_generation_prompt=True,
                    )
                    inputs = tokenizer(
                        rendered,
                        return_tensors="pt",
                        padding=True,
                        add_special_tokens=False,
                    ).to(input_device)
                    prompt_tokens = int(inputs["input_ids"].shape[1])
                    with torch.inference_mode():
                        sequences = model.generate(**inputs, **generation_options)
                    generated_rows = sequences[:, prompt_tokens:].detach().cpu().tolist()

                    for offset, row in enumerate(generated_rows, start=1):
                        sample_number = start + offset
                        sample_id = followup_sample_id(
                            source_turn,
                            followup_prompt_id=followup.prompt_id,
                            followup_condition=followup.condition,
                            sample_number=sample_number,
                        )
                        token_ids = trim_generated_tokens(row, endings)
                        if sample_id in completed_ids:
                            continue
                        total_generated_tokens += len(token_ids)
                        parsed = parse_tokens(
                            token_ids,
                            tokenizer=tokenizer,
                            reasoning_end_marker=args.reasoning_end_marker,
                            eos_ids=endings,
                        )
                        messages = message_batches[offset - 1]
                        record = {
                            "sample_id": sample_id,
                            "sample_number": sample_number,
                            "source": {
                                "jsonl": str(source_path),
                                "jsonl_sha256": source_hash,
                                "row": source_turn.row_number,
                                "sample_id": source_turn.record["sample_id"],
                                "prompt_id": source_turn.record.get("prompt_id"),
                                "condition": source_turn.record.get("condition"),
                                "sample_number": source_turn.record.get("sample_number"),
                            },
                            "followup": {
                                "prompt_file": str(prompts_path),
                                "prompt_file_sha256": prompt_hash,
                                "prompt_id": followup.prompt_id,
                                "level": followup.level,
                                "level_label": followup.level_label,
                                "title": followup.title,
                                "condition": followup.condition,
                                "prompt": followup.prompt,
                            },
                            "messages": messages,
                            "conversation": [
                                *messages,
                                {"role": "assistant", "content": parsed["response"]},
                            ],
                            "batch_seed": batch_seed,
                            "prompt_tokens": prompt_tokens,
                            **parsed,
                            "generation_config": generation_config,
                        }
                        output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                        completed_ids.add(sample_id)
                        completed += 1
                        records_written += 1
                    output_file.flush()
                    del inputs, sequences, generated_rows
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        print(
            "CUDA ran out of memory. The JSONL retains completed batches; rerun "
            "the same command with --resume.",
            file=sys.stderr,
        )
        return 2

    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    rate = total_generated_tokens / elapsed if elapsed else 0.0
    print("\n=== Complete ===")
    print(f"New records:      {records_written}")
    print(f"Total records:    {completed}/{total_samples}")
    print(f"Generated tokens: {total_generated_tokens}")
    print(f"Generation time:  {elapsed:.2f} s")
    print(f"Throughput:       {rate:.2f} tokens/s")
    print(f"Output:           {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
