#!/usr/bin/env python3
"""Run the flipped small experiment: saved L3 09 turns followed by B7."""

from __future__ import annotations

import sys
from typing import Sequence

from two_turn_batch_qwen import parse_args as parse_batch_args
from two_turn_batch_qwen import run


def parse_args(argv: Sequence[str] | None = None):
    """Apply the flipped prompt defaults while retaining all batch-runner options."""

    user_args = list(sys.argv[1:] if argv is None else argv)
    flipped_defaults = [
        "--source-prompt-id",
        "L3_09",
        "--followup-prompt-id",
        "B7",
    ]
    return parse_batch_args([*flipped_defaults, *user_args])


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
