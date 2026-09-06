#!/usr/bin/env python3

from __future__ import annotations

exp = "17"
source = "L4_15"
followup = "B7"

import sys
from typing import Sequence

from two_turn_batch_qwen import parse_args as parse_batch_args
from two_turn_batch_qwen import run


def parse_args(argv: Sequence[str] | None = None):
    """Apply the flipped prompt defaults while retaining all batch-runner options."""

    user_args = list(sys.argv[1:] if argv is None else argv)
    flipped_defaults = [
        "--source-prompt-id",
        source,
        "--followup-prompt-id",
        followup,
        "--temperature",
        "1.0",
        "--output",
        f"outputs/qwen35_criticism_{source}_to_{followup}_exp{exp}.jsonl",
    ]
    return parse_batch_args([*flipped_defaults, *user_args])


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
