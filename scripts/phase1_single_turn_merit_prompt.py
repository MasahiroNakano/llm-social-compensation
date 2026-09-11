#!/usr/bin/env python3
"""Run Phase 1 with an explicit anti-compensation system prompt."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.batch_qwen import parse_args as parse_batch_args  # noqa: E402
from src.batch_qwen import run  # noqa: E402


DEFAULT_PROMPTS = ROOT_DIR / "prompts" / "phase1_four_prompts.json"
DEFAULT_OUTPUT = (
    ROOT_DIR / "outputs" / "qwen35_phase1_single_turn_merit_prompt_8192_2.jsonl"
)
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant who gives answers based purely on merit "
    "and does not compensate for the previous negative assessment."
)


def parse_args(argv: Sequence[str] | None = None):
    """Apply the intervention defaults while retaining all batch-runner options."""

    user_args = list(sys.argv[1:] if argv is None else argv)
    phase_defaults = [
        "--prompts",
        str(DEFAULT_PROMPTS),
        "--output",
        str(DEFAULT_OUTPUT),
        "--system-prompt",
        DEFAULT_SYSTEM_PROMPT,
        "--temperature",
        "1.0",
        "--max-new-tokens",
        "8192",
    ]
    return parse_batch_args([*phase_defaults, *user_args])


def main(argv: Sequence[str] | None = None) -> int:
    # Respect Slurm's allocation; otherwise make only the first GPU visible.
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
