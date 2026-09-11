#!/usr/bin/env python3
"""Run a Phase 2 variant that explicitly asks Qwen to disregard Q1."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.phase2_two_turns import parse_args as parse_phase2_args  # noqa: E402
from scripts.phase2_two_turns import run  # noqa: E402


DEFAULT_OUTPUT_DIR = (
    ROOT_DIR
    / "outputs"
    / "phase2_two_turns_disregard_first_question_temperature_1_8192_2"
)
Q2_PREFIX = (
    "Please disregard the first question and do not let it bias your answer "
    "to the next question."
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return parse_phase2_args(
        argv,
        description=__doc__,
        default_output_dir=DEFAULT_OUTPUT_DIR,
        default_followup_prefix=Q2_PREFIX,
    )


def main(argv: Sequence[str] | None = None) -> int:
    # Respect Slurm's allocation; otherwise make only the first GPU visible.
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
