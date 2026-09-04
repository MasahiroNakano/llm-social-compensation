"""Fast tests for the fixed-follow-up batch runner; no GPU is required."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from two_turn_batch_qwen import (
    common_source_setting,
    expected_samples,
    followup_sample_id,
    load_source_turns,
    output_path,
    repeated_messages,
    stable_batch_seed,
)


def source_record(sample_id: str, sample_number: int) -> dict:
    return {
        "sample_id": sample_id,
        "prompt_id": "L3_11",
        "condition": "natural",
        "sample_number": sample_number,
        "prompt": "first user",
        "response": f"first assistant {sample_number}",
        "generation_config": {"model": "test/model", "seed": 0},
    }


class TwoTurnBatchQwenTests(unittest.TestCase):
    def test_load_source_turns_selects_requested_records_and_rows(self) -> None:
        rows = [
            source_record("other.natural.s01", 1) | {"prompt_id": "L1_01"},
            source_record("L3_11.natural.s01", 1),
            source_record("L3_11.natural.s02", 2),
        ]
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            selected = load_source_turns(
                path,
                prompt_id="L3_11",
                condition="natural",
                expected_count=2,
            )
        self.assertEqual([turn.row_number for turn in selected], [2, 3])
        self.assertEqual(
            [turn.record["sample_id"] for turn in selected],
            ["L3_11.natural.s01", "L3_11.natural.s02"],
        )

    def test_load_source_turns_enforces_expected_count(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text(
                json.dumps(source_record("L3_11.natural.s01", 1)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Expected 2"):
                load_source_turns(
                    path,
                    prompt_id="L3_11",
                    condition="natural",
                    expected_count=2,
                )

    def test_expected_samples_builds_full_cross_product(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(source_record(f"L3_11.natural.s{i:02d}", i))
                    for i in range(1, 3)
                ) + "\n",
                encoding="utf-8",
            )
            turns = load_source_turns(
                path,
                prompt_id="L3_11",
                condition="natural",
                expected_count=2,
            )
        expected = expected_samples(
            turns,
            followup_prompt_id="L3_09",
            followup_condition="natural",
            samples_per_source=8,
        )
        self.assertEqual(len(expected), 16)
        self.assertIn("L3_11.natural.s02__to__L3_09.natural.s08", expected)

    def test_followup_sample_id_is_unambiguous(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.jsonl"
            path.write_text(
                json.dumps(source_record("L3_11.natural.s07", 7)) + "\n",
                encoding="utf-8",
            )
            turn = load_source_turns(
                path,
                prompt_id="L3_11",
                condition="natural",
                expected_count=1,
            )[0]
        self.assertEqual(
            followup_sample_id(
                turn,
                followup_prompt_id="L3_09",
                followup_condition="natural",
                sample_number=3,
            ),
            "L3_11.natural.s07__to__L3_09.natural.s03",
        )

    def test_common_setting_requires_consistency_without_override(self) -> None:
        from two_turn_batch_qwen import SourceTurn

        left = SourceTurn(1, source_record("left", 1))
        right_record = source_record("right", 2)
        right_record["generation_config"]["model"] = "other/model"
        right = SourceTurn(2, right_record)
        with self.assertRaisesRegex(ValueError, "disagree"):
            common_source_setting(
                [left, right], name="model", override=None, fallback="fallback"
            )
        self.assertEqual(
            common_source_setting(
                [left, right], name="model", override="chosen/model", fallback="fallback"
            ),
            "chosen/model",
        )

    def test_repeated_messages_preserves_role_order(self) -> None:
        from two_turn_batch_qwen import SourceTurn

        turn = SourceTurn(1, source_record("sample", 1))
        batches = repeated_messages(
            turn,
            followup_prompt="second user",
            system_prompt="system",
            count=3,
        )
        self.assertEqual(len(batches), 3)
        self.assertEqual(
            [message["role"] for message in batches[0]],
            ["system", "user", "assistant", "user"],
        )

    def test_stable_seed_depends_on_source_and_batch(self) -> None:
        args = (0, "source-a", "L3_09", "natural")
        self.assertEqual(
            stable_batch_seed(*args, 0, 8),
            stable_batch_seed(*args, 0, 8),
        )
        self.assertNotEqual(
            stable_batch_seed(*args, 0, 8),
            stable_batch_seed(0, "source-b", "L3_09", "natural", 0, 8),
        )

    def test_resume_requires_explicit_existing_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit"):
            output_path(None, resume=True)
        with TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.jsonl"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                output_path(missing, resume=True)


if __name__ == "__main__":
    unittest.main()
