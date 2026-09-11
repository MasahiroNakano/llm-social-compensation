#!/usr/bin/env python3
"""Run Phase 2 with an explicit anti-compensation system prompt."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.qwen_runtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_REASONING_END_MARKER,
)
from src.batch_qwen import (  # noqa: E402
    load_runtime,
    repair_incomplete_jsonl_tail,
)
from src.two_turn_batch_qwen import parse_args as parse_pair_args  # noqa: E402
from src.two_turn_batch_qwen import run as run_pair  # noqa: E402


DEFAULT_PROMPTS = ROOT_DIR / "prompts" / "phase1_four_prompts.json"
DEFAULT_SOURCE_JSONL = (
    ROOT_DIR / "outputs" / "qwen35_phase1_single_turn_merit_prompt_8192_2.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    ROOT_DIR
    / "outputs"
    / "phase2_two_turns_merit_prompt_temperature_1_8192_2"
)
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant who gives answers based purely on merit "
    "and does not compensate for the previous negative assessment."
)
PROMPT_IDS = ("P1", "P2", "P3", "P4")
ORDERED_PAIRS = tuple(
    (source, followup)
    for source in PROMPT_IDS
    for followup in PROMPT_IDS
    if source != followup
)


def pair_value(value: str) -> tuple[str, str]:
    """Parse an optional SOURCE:FOLLOWUP pair selector."""

    try:
        source, followup = value.split(":", maxsplit=1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "pair must have the form SOURCE:FOLLOWUP (for example, P1:P2)"
        ) from exc
    pair = (source, followup)
    if pair not in ORDERED_PAIRS:
        allowed = ", ".join(f"{left}:{right}" for left, right in ORDERED_PAIRS)
        raise argparse.ArgumentTypeError(f"unknown pair {value!r}; choose from {allowed}")
    return pair


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-jsonl", type=Path, default=DEFAULT_SOURCE_JSONL)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--pair",
        action="append",
        type=pair_value,
        dest="pairs",
        help=(
            "Run only SOURCE:FOLLOWUP; repeat to select multiple pairs. "
            "By default all 12 ordered pairs run."
        ),
    )
    parser.add_argument(
        "--source-condition",
        choices=("natural", "criticism_eliciting"),
        default="natural",
    )
    parser.add_argument(
        "--followup-condition",
        choices=("natural", "criticism_eliciting"),
        default="natural",
    )
    parser.add_argument("--expected-source-count", type=int, default=16)
    parser.add_argument("--samples-per-source", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help=(
            "System prompt used for both turns. Defaults to the anti-compensation "
            "intervention prompt."
        ),
    )
    parser.add_argument("--max-new-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=1.0)
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
        help=(
            "Validate and continue existing pair outputs; pairs without an output "
            "file start from the beginning."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview every selected pair without loading Qwen.",
    )
    return parser.parse_args(argv)


def selected_pairs(args: argparse.Namespace) -> tuple[tuple[str, str], ...]:
    if not args.pairs:
        return ORDERED_PAIRS
    # Preserve CLI order while preventing an accidental duplicate experiment.
    return tuple(dict.fromkeys(args.pairs))


def pair_output_path(
    output_dir: Path,
    source_prompt_id: str,
    followup_prompt_id: str,
) -> Path:
    return output_dir / f"qwen35_phase2_{source_prompt_id}_to_{followup_prompt_id}.jsonl"


def namespace_for_pair(
    args: argparse.Namespace,
    pair: tuple[str, str],
    *,
    resume: bool,
) -> argparse.Namespace:
    """Translate sweep options into one invocation of the existing runner."""

    source, followup = pair
    pair_argv = [
        "--source-jsonl",
        str(args.source_jsonl),
        "--source-prompt-id",
        source,
        "--source-condition",
        args.source_condition,
        "--expected-source-count",
        str(args.expected_source_count),
        "--prompts",
        str(args.prompts),
        "--followup-prompt-id",
        followup,
        "--followup-condition",
        args.followup_condition,
        "--samples-per-source",
        str(args.samples_per_source),
        "--batch-size",
        str(args.batch_size),
        "--output",
        str(pair_output_path(args.output_dir, source, followup)),
        "--model",
        args.model,
        "--system-prompt",
        args.system_prompt,
        "--max-new-tokens",
        str(args.max_new_tokens),
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--seed",
        str(args.seed),
        "--reasoning-end-marker",
        args.reasoning_end_marker,
    ]
    if args.cache_dir is not None:
        pair_argv.extend(("--cache-dir", str(args.cache_dir)))
    if resume:
        pair_argv.append("--resume")
    if args.dry_run:
        pair_argv.append("--dry-run")
    return parse_pair_args(pair_argv)


def nonblank_line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as input_file:
        return sum(bool(line.strip()) for line in input_file)


def run(args: argparse.Namespace) -> int:
    pairs = selected_pairs(args)
    output_dir = args.output_dir.expanduser().resolve()
    args.output_dir = output_dir
    args.source_jsonl = args.source_jsonl.expanduser().resolve()
    args.prompts = args.prompts.expanduser().resolve()

    outputs = {
        pair: pair_output_path(output_dir, *pair).resolve()
        for pair in pairs
    }
    if not args.resume and not args.dry_run:
        existing = [path for path in outputs.values() if path.exists()]
        if existing:
            print(
                "Error: refusing to overwrite existing phase-two output(s); "
                "rerun with --resume:\n  " + "\n  ".join(map(str, existing)),
                file=sys.stderr,
            )
            return 1

    print(
        f"Prepared {len(pairs)} ordered pair(s): "
        + ", ".join(f"{source}->{followup}" for source, followup in pairs)
    )

    if args.dry_run:
        for index, pair in enumerate(pairs, start=1):
            print(f"\n=== Pair {index}/{len(pairs)}: {pair[0]} -> {pair[1]} ===")
            result = run_pair(namespace_for_pair(args, pair, resume=False))
            if result:
                return result
        return 0

    # Validate every source selection, prompt, and generation setting before
    # spending time loading the model. The underlying dry run performs no writes.
    for pair in pairs:
        preflight_args = namespace_for_pair(args, pair, resume=False)
        preflight_args.dry_run = True
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_pair(preflight_args)
        if result:
            print(
                f"Phase-two preflight failed for {pair[0]} -> {pair[1]}.",
                file=sys.stderr,
            )
            return result

    pending: list[tuple[tuple[str, str], argparse.Namespace]] = []
    expected_records = args.expected_source_count * args.samples_per_source
    for index, pair in enumerate(pairs, start=1):
        output = outputs[pair]
        should_resume = args.resume and output.exists()
        pair_args = namespace_for_pair(args, pair, resume=should_resume)
        if should_resume:
            repair_message = repair_incomplete_jsonl_tail(output)
            if repair_message:
                print(f"Resume repair for {pair[0]} -> {pair[1]}: {repair_message}")
        if should_resume and nonblank_line_count(output) >= expected_records:
            print(f"\n=== Validating pair {index}/{len(pairs)}: {pair[0]} -> {pair[1]} ===")
            result = run_pair(pair_args)
            if result:
                return result
        else:
            pending.append((pair, pair_args))

    if not pending:
        print("All selected phase-two pairs are complete.")
        return 0

    runtime_args = argparse.Namespace(
        cache_dir=args.cache_dir,
        model=args.model,
        seed=args.seed,
    )
    try:
        runtime: tuple[Any, Any, Any] = load_runtime(runtime_args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for index, (pair, pair_args) in enumerate(pending, start=1):
        print(
            f"\n=== Running unfinished pair {index}/{len(pending)}: "
            f"{pair[0]} -> {pair[1]} ==="
        )
        result = run_pair(pair_args, runtime=runtime)
        if result:
            return result
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # Respect Slurm's allocation; otherwise make only the first GPU visible.
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
