"""Tests for the flipped L3_09-to-L3_11 batch-runner configuration."""

from __future__ import annotations

import unittest

from two_turn_batch_qwen_2 import parse_args


class FlippedTwoTurnBatchQwenTests(unittest.TestCase):
    def test_flipped_defaults(self) -> None:
        args = parse_args([])
        self.assertEqual(args.source_prompt_id, "L3_09")
        self.assertEqual(args.followup_prompt_id, "L3_11")
        self.assertEqual(args.source_condition, "natural")
        self.assertEqual(args.followup_condition, "natural")
        self.assertEqual(args.expected_source_count, 16)
        self.assertEqual(args.samples_per_source, 8)
        self.assertEqual(args.batch_size, 8)

    def test_command_line_can_still_override_defaults(self) -> None:
        args = parse_args(
            [
                "--source-prompt-id",
                "custom_first",
                "--followup-prompt-id",
                "custom_second",
            ]
        )
        self.assertEqual(args.source_prompt_id, "custom_first")
        self.assertEqual(args.followup_prompt_id, "custom_second")


if __name__ == "__main__":
    unittest.main()
