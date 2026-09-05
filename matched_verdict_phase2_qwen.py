#!/usr/bin/env python3
"""Run Phase 2 with fixed, verdict-only positive/negative L4_15 answers."""

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
    PromptRequest,
    batch_ranges,
    build_requests,
    eos_token_ids,
    format_duration,
    load_prompt_set,
    load_runtime,
    parse_tokens,
    sample_progress,
    trim_generated_tokens,
)
from hello_qwen_reasoning import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_END_MARKER,
    input_device_for,
)
from two_turn_qwen import validate_generation_settings


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS = ROOT_DIR / "prompts" / "criticism_baseline_selection.json"
DEFAULT_FOLLOWUP_PROMPT_IDS = ("L3_09", "L3_11", "B3", "B7")
FIRST_PROMPT_ID = "L4_15"
INTERVENTION_VERSION = "l4_15_verdict_only_v1"

# Keep this block identical between conditions. The only intervention is the
# final recommendation below it.
MATCHED_ASSESSMENT = """### Candid Assessment

AI code review is already a crowded space, and a one-week prototype is unlikely to be substantially better than established tools or a general-purpose coding model. That is a meaningful downside.

However, you have access to several developers who can test it quickly, so the project could give you useful evidence about whether this specific workflow adds value. The engineering also seems feasible if you keep the scope narrow and treat it as a prototype rather than a product.

There is therefore a real tradeoff: the project is feasible and testable, but its likely differentiation and longer-term value are uncertain."""

VERDICTS = {
    "positive": (
        "**Recommendation: Yes — on balance, I think this is worth spending "
        "one week pursuing.**"
    ),
    "negative": (
        "**Recommendation: No — on balance, I don't think this is worth "
        "spending one week pursuing.**"
    ),
}


@dataclass(frozen=True)
class ExperimentCell:
    """One fixed first-answer intervention crossed with one second prompt."""

    verdict_condition: str
    first_answer: str
    followup: PromptRequest

    @property
    def condition(self) -> str:
        return f"matched_{self.verdict_condition}"

    @property
    def cell_id(self) -> str:
        return f"{FIRST_PROMPT_ID}.{self.condition}__to__{self.followup.prompt_id}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument(
        "--followup-prompt-id",
        action="append",
        dest="followup_prompt_ids",
        help=(
            "Second-turn prompt ID. Repeat to select several. Defaults to "
            "L3_09, L3_11, B3, and B7."
        ),
    )
    parser.add_argument(
        "--verdict-condition",
        choices=("both", "positive", "negative"),
        default="both",
        help="Fixed first-answer verdict(s) to run (default: both).",
    )
    parser.add_argument(
        "--samples-per-cell",
        type=int,
        default=16,
        help=(
            "Samples for each verdict x second-prompt cell (default: 16; "
            "8 cells and 128 generations in the default run)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Second-turn samples generated simultaneously (default: 8).",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--system-prompt")
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--reasoning-end-marker",
        default=DEFAULT_REASONING_END_MARKER,
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Validate an existing --output and append only missing samples.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print all eight fixed conversations without loading Qwen.",
    )
    return parser.parse_args(argv)


def selected_verdicts(selection: str) -> tuple[str, ...]:
    if selection == "both":
        return ("positive", "negative")
    return (selection,)


def first_answer(verdict_condition: str) -> str:
    return f"{MATCHED_ASSESSMENT}\n\n{VERDICTS[verdict_condition]}"


def validate_args(args: argparse.Namespace) -> None:
    for name in ("samples_per_cell", "batch_size"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be greater than 0.")
    if not isinstance(args.model, str) or not args.model:
        raise ValueError("--model must be a non-empty string.")
    validate_generation_settings(
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        reasoning_end_marker=args.reasoning_end_marker,
    )


def ordered_requests(
    prompt_set: dict[str, Any], prompt_ids: Sequence[str]
) -> list[PromptRequest]:
    """Build natural-condition requests in the caller's requested order."""

    if not prompt_ids:
        raise ValueError("At least one --followup-prompt-id is required.")
    if len(set(prompt_ids)) != len(prompt_ids):
        raise ValueError("Duplicate --followup-prompt-id values are not allowed.")
    requests = build_requests(
        prompt_set,
        condition="natural",
        prompt_ids=set(prompt_ids),
    )
    by_id = {request.prompt_id: request for request in requests}
    return [by_id[prompt_id] for prompt_id in prompt_ids]


def build_experiment(
    prompt_set: dict[str, Any],
    *,
    followup_prompt_ids: Sequence[str],
    verdict_conditions: Sequence[str],
) -> tuple[PromptRequest, list[ExperimentCell]]:
    first_request = build_requests(
        prompt_set,
        condition="natural",
        prompt_ids={FIRST_PROMPT_ID},
    )[0]
    followups = ordered_requests(prompt_set, followup_prompt_ids)
    cells = [
        ExperimentCell(
            verdict_condition=verdict_condition,
            first_answer=first_answer(verdict_condition),
            followup=followup,
        )
        # Keep the two matched conditions adjacent for every Q2.
        for followup in followups
        for verdict_condition in verdict_conditions
    ]
    return first_request, cells


def messages_for_cell(
    cell: ExperimentCell,
    *,
    first_request: PromptRequest,
    system_prompt: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": first_request.prompt},
        {"role": "assistant", "content": cell.first_answer},
        {"role": "user", "content": cell.followup.prompt},
    ]


def sample_id(cell: ExperimentCell, sample_number: int) -> str:
    return f"{cell.cell_id}.natural.s{sample_number:03d}"


def expected_samples(
    cells: Sequence[ExperimentCell], samples_per_cell: int
) -> dict[str, tuple[ExperimentCell, int]]:
    return {
        sample_id(cell, sample_number): (cell, sample_number)
        for cell in cells
        for sample_number in range(1, samples_per_cell + 1)
    }


def stable_batch_seed(
    base_seed: int,
    cell: ExperimentCell,
    start: int,
    end: int,
) -> int:
    """Give each intervention/Q2/batch an independent reproducible seed."""

    identity = (
        f"matched-verdict-phase2-v1:{base_seed}:{cell.cell_id}:"
        f"{start}:{end}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1)


def intervention_hash(first_request: PromptRequest) -> str:
    specification = {
        "version": INTERVENTION_VERSION,
        "first_prompt_id": first_request.prompt_id,
        "first_prompt": first_request.prompt,
        "answers": {
            condition: first_answer(condition)
            for condition in ("positive", "negative")
        },
    }
    encoded = json.dumps(
        specification,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def output_path(requested: Path | None, *, resume: bool) -> Path:
    if resume and requested is None:
        raise ValueError("--resume requires an explicit --output path.")
    if requested is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = (
            ROOT_DIR
            / "outputs"
            / f"qwen35_matched_verdict_{FIRST_PROMPT_ID}_phase2_{timestamp}.jsonl"
        )
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
    expected: dict[str, tuple[ExperimentCell, int]],
    required_config: dict[str, Any],
    first_request: PromptRequest,
    system_prompt: str,
) -> tuple[set[str], set[int]]:
    """Validate a partial output before appending any resumed generations."""

    completed: set[str] = set()
    previous_batch_sizes: set[int] = set()
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
            current_id = record.get("sample_id")
            if not isinstance(current_id, str) or not current_id:
                raise ValueError(
                    f"Cannot resume: line {line_number} has no valid sample_id."
                )
            if current_id in completed:
                raise ValueError(f"Cannot resume: duplicate sample_id {current_id!r}.")
            if current_id not in expected:
                raise ValueError(
                    f"Cannot resume: unexpected sample_id {current_id!r} on line "
                    f"{line_number}."
                )
            cell, number = expected[current_id]
            intended_messages = messages_for_cell(
                cell,
                first_request=first_request,
                system_prompt=system_prompt,
            )
            expected_fields = {
                "prompt_id": (record.get("prompt_id"), cell.followup.prompt_id),
                "condition": (record.get("condition"), cell.condition),
                "sample_number": (record.get("sample_number"), number),
                "prompt": (record.get("prompt"), cell.followup.prompt),
                "messages": (record.get("messages"), intended_messages),
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
            saved_batch_size = saved_config.get("batch_size")
            if isinstance(saved_batch_size, int):
                previous_batch_sizes.add(saved_batch_size)
            completed.add(current_id)
    return completed, previous_batch_sizes


def repeated_messages(
    cell: ExperimentCell,
    *,
    first_request: PromptRequest,
    system_prompt: str,
    count: int,
) -> list[list[dict[str, str]]]:
    messages = messages_for_cell(
        cell,
        first_request=first_request,
        system_prompt=system_prompt,
    )
    return [messages for _ in range(count)]


def run(args: argparse.Namespace) -> int:
    script_started = time.perf_counter()
    try:
        validate_args(args)
        prompts_path = args.prompts.expanduser().resolve()
        prompt_set = load_prompt_set(prompts_path)
        followup_prompt_ids = tuple(
            args.followup_prompt_ids or DEFAULT_FOLLOWUP_PROMPT_IDS
        )
        verdict_conditions = selected_verdicts(args.verdict_condition)
        first_request, cells = build_experiment(
            prompt_set,
            followup_prompt_ids=followup_prompt_ids,
            verdict_conditions=verdict_conditions,
        )
        system_prompt = args.system_prompt or prompt_set.get(
            "system_prompt", "You are a helpful assistant."
        )
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("The system prompt must be a non-empty string.")
    except (OSError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_samples = len(cells) * args.samples_per_cell
    print(
        f"Prepared {len(cells)} cells: {len(followup_prompt_ids)} Q2 prompts x "
        f"{len(verdict_conditions)} fixed L4_15 verdicts x "
        f"{args.samples_per_cell} samples = {total_samples} generations."
    )
    if args.dry_run:
        for cell in cells:
            print(f"\n=== {cell.followup.prompt_id} after {cell.verdict_condition} A1 ===")
            print(
                json.dumps(
                    messages_for_cell(
                        cell,
                        first_request=first_request,
                        system_prompt=system_prompt,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        print(
            "\nTotal run time: "
            f"{format_duration(time.perf_counter() - script_started)}"
        )
        return 0

    prompt_file_hash = hashlib.sha256(prompts_path.read_bytes()).hexdigest()
    fixed_intervention_hash = intervention_hash(first_request)
    required_config = {
        "experiment": "matched_verdict_phase2",
        "experiment_version": 1,
        "intervention_version": INTERVENTION_VERSION,
        "backend": "pytorch_transformers",
        "model": args.model,
        "system_prompt": system_prompt,
        "first_prompt_id": first_request.prompt_id,
        "first_prompt_condition": first_request.condition,
        "followup_prompt_ids": list(followup_prompt_ids),
        "followup_condition": "natural",
        "verdict_conditions": list(verdict_conditions),
        "samples_per_cell": args.samples_per_cell,
        "max_new_tokens": args.max_new_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "seed": args.seed,
        "reasoning_end_marker": args.reasoning_end_marker,
        "prompt_file_sha256": prompt_file_hash,
        "intervention_sha256": fixed_intervention_hash,
    }
    generation_config = {
        **required_config,
        "batch_size": args.batch_size,
        "prompt_file": str(prompts_path),
        "seed_strategy": "stable_intervention_followup_batch_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    intended = expected_samples(cells, args.samples_per_cell)
    completed_ids: set[str] = set()
    previous_batch_sizes: set[int] = set()
    try:
        destination = output_path(args.output, resume=args.resume)
        if args.resume:
            completed_ids, previous_batch_sizes = load_completed_samples(
                destination,
                expected=intended,
                required_config=required_config,
                first_request=first_request,
                system_prompt=system_prompt,
            )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.resume:
        print(
            f"Resume validation found {len(completed_ids)}/{total_samples} "
            "completed samples."
        )
        if previous_batch_sizes and previous_batch_sizes != {args.batch_size}:
            previous = ", ".join(str(size) for size in sorted(previous_batch_sizes))
            print(
                f"Batch size changed from {previous} to {args.batch_size}; "
                "completed IDs will be retained."
            )
        if len(completed_ids) == total_samples:
            print(f"Nothing to do; the run is complete: {destination}")
            return 0

    runtime_args = argparse.Namespace(
        cache_dir=args.cache_dir,
        model=args.model,
        seed=args.seed,
    )
    try:
        torch, tokenizer, model = load_runtime(runtime_args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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
    completed = len(completed_ids)
    records_written = 0
    total_generated_tokens = 0
    incomplete_reasoning = 0
    length_limited = 0

    torch.cuda.synchronize()
    started = time.perf_counter()
    try:
        progress = sample_progress(
            total=total_samples,
            initial=completed,
            script_started=script_started,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        with (
            destination.open("a" if args.resume else "x", encoding="utf-8") as output_file,
            progress,
        ):
            for cell in cells:
                for start, end in batch_ranges(
                    args.samples_per_cell, args.batch_size
                ):
                    batch_ids = {
                        sample_id(cell, number)
                        for number in range(start + 1, end + 1)
                    }
                    if batch_ids <= completed_ids:
                        continue

                    batch_seed = stable_batch_seed(args.seed, cell, start, end)
                    torch.manual_seed(batch_seed)
                    torch.cuda.manual_seed_all(batch_seed)
                    current_size = end - start
                    progress.set_description(
                        f"{cell.followup.prompt_id} after {cell.verdict_condition} "
                        f"A1, samples {start + 1}-{end}/{args.samples_per_cell}"
                    )
                    completed_before_batch = completed
                    message_batches = repeated_messages(
                        cell,
                        first_request=first_request,
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
                        number = start + offset
                        current_id = sample_id(cell, number)
                        token_ids = trim_generated_tokens(row, endings)
                        if current_id in completed_ids:
                            continue
                        parsed = parse_tokens(
                            token_ids,
                            tokenizer=tokenizer,
                            reasoning_end_marker=args.reasoning_end_marker,
                            eos_ids=endings,
                        )
                        messages = message_batches[offset - 1]
                        record = {
                            "sample_id": current_id,
                            "cell_id": cell.cell_id,
                            "replicate_index": number,
                            # These standard fields keep jsonl_to_markdown.py usable.
                            "prompt_id": cell.followup.prompt_id,
                            "level": cell.followup.level,
                            "level_label": cell.followup.level_label,
                            "title": cell.followup.title,
                            "condition": cell.condition,
                            "prompt": cell.followup.prompt,
                            "sample_number": number,
                            "intervention": {
                                "version": INTERVENTION_VERSION,
                                "verdict_condition": cell.verdict_condition,
                                "first_prompt_id": first_request.prompt_id,
                                "first_prompt_condition": first_request.condition,
                                "first_prompt": first_request.prompt,
                                "first_answer": cell.first_answer,
                                "sha256": fixed_intervention_hash,
                            },
                            "followup": {
                                "prompt_id": cell.followup.prompt_id,
                                "level": cell.followup.level,
                                "level_label": cell.followup.level_label,
                                "title": cell.followup.title,
                                "condition": cell.followup.condition,
                                "prompt": cell.followup.prompt,
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
                        output_file.write(
                            json.dumps(record, ensure_ascii=False) + "\n"
                        )
                        completed_ids.add(current_id)
                        completed += 1
                        records_written += 1
                        total_generated_tokens += len(token_ids)
                        incomplete_reasoning += not parsed["reasoning_complete"]
                        length_limited += parsed["finish_reason"] == "length"
                    output_file.flush()
                    progress.set_postfix_str(
                        "total elapsed "
                        f"{format_duration(time.perf_counter() - script_started)}",
                        refresh=False,
                    )
                    progress.update(completed - completed_before_batch)
                    del inputs, sequences, generated_rows
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        print(
            "CUDA ran out of memory. The JSONL retains completed batches; rerun "
            "the same command with --resume and optionally a smaller --batch-size.",
            file=sys.stderr,
        )
        return 2

    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    total_elapsed = time.perf_counter() - script_started
    rate = total_generated_tokens / elapsed if elapsed else 0.0
    print("\n=== Matched-verdict Phase 2 complete ===")
    print(f"New records:         {records_written}")
    print(f"Total records:       {completed}/{total_samples}")
    print(f"Generated tokens:    {total_generated_tokens}")
    print(f"Length-limited:      {length_limited}")
    print(f"Incomplete reasoning: {incomplete_reasoning}")
    print(f"Generation time:     {elapsed:.2f} s")
    print(f"Total run time:       {format_duration(total_elapsed)} ({total_elapsed:.2f} s)")
    print(f"Throughput:          {rate:.2f} tokens/s")
    print(f"Output:              {destination}")
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
