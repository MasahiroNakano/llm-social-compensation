"""Fast tests for the two-turn Qwen smoke-test runner; no GPU is required."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from two_turn_qwen import (
    build_messages,
    load_jsonl_row,
    output_path,
    source_setting,
)


class TwoTurnQwenTests(unittest.TestCase):
    def test_load_jsonl_row_counts_nonblank_records_from_one(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text('{"id": 1}\n\n{"id": 2}\n', encoding="utf-8")
            self.assertEqual(load_jsonl_row(path, 2), {"id": 2})

    def test_load_jsonl_row_rejects_out_of_range_row(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text('{"id": 1}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fewer than 2"):
                load_jsonl_row(path, 2)

    def test_build_messages_uses_only_saved_final_response_as_history(self) -> None:
        source = {
            "prompt": "first user",
            "response": "first assistant",
            "reasoning": "do not include this",
        }
        self.assertEqual(
            build_messages(
                source_record=source,
                followup_prompt="second user",
                system_prompt="system",
            ),
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "first user"},
                {"role": "assistant", "content": "first assistant"},
                {"role": "user", "content": "second user"},
            ],
        )

    def test_source_setting_prefers_cli_override(self) -> None:
        args = argparse.Namespace(seed=7)
        self.assertEqual(source_setting(args, {"seed": 3}, "seed", 0), 7)
        args.seed = None
        self.assertEqual(source_setting(args, {"seed": 3}, "seed", 0), 3)

    def test_output_path_refuses_to_overwrite(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "existing.jsonl"
            path.write_text(json.dumps({"complete": True}) + "\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                output_path(path)


if __name__ == "__main__":
    unittest.main()
