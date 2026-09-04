"""Fast tests for the criticism-baseline batch runner; no GPU is required."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from batch_qwen import (
    DEFAULT_PROMPTS,
    batch_ranges,
    build_requests,
    expected_samples,
    format_duration,
    load_completed_samples,
    load_prompt_set,
    output_path,
    parse_tokens,
    stable_batch_seed,
)


class FakeTokenizer:
    unk_token_id = 0

    def convert_tokens_to_ids(self, token: str) -> int:
        return 2 if token == "</think>" else self.unk_token_id

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        del text, add_special_tokens
        return []

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        pieces = {1: "reasoning", 2: "</think>", 3: "final", 4: "<eos>"}
        special_ids = {2, 4} if skip_special_tokens else set()
        return "".join(pieces[token] for token in token_ids if token not in special_ids)


class PromptSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt_set = load_prompt_set(DEFAULT_PROMPTS)

    def test_all_eighteen_prompts_are_present(self) -> None:
        self.assertEqual(len(self.prompt_set["prompts"]), 18)
        self.assertEqual(
            [prompt["level"] for prompt in self.prompt_set["prompts"]],
            [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4 + [5] * 2,
        )

    def test_natural_condition_creates_eighteen_requests(self) -> None:
        requests = build_requests(self.prompt_set, condition="natural")
        self.assertEqual(len(requests), 18)
        self.assertTrue(requests[0].prompt.endswith("a direct recommendation."))

    def test_both_conditions_create_thirty_six_requests(self) -> None:
        requests = build_requests(self.prompt_set, condition="both")
        self.assertEqual(len(requests), 36)
        self.assertEqual(
            {request.condition for request in requests},
            {"natural", "criticism_eliciting"},
        )

    def test_prompt_filter(self) -> None:
        requests = build_requests(
            self.prompt_set,
            condition="natural",
            prompt_ids={"L4_13"},
        )
        self.assertEqual([request.prompt_id for request in requests], ["L4_13"])

    def test_manual_batch_ranges_include_short_final_batch(self) -> None:
        self.assertEqual(list(batch_ranges(16, 6)), [(0, 6), (6, 12), (12, 16)])

    def test_elapsed_duration_uses_fixed_width_clock_format(self) -> None:
        self.assertEqual(format_duration(0), "00:00:00")
        self.assertEqual(format_duration(65.9), "00:01:05")
        self.assertEqual(format_duration(3_661.2), "01:01:01")

    def test_batch_seed_is_stable_and_batch_specific(self) -> None:
        request = build_requests(
            self.prompt_set,
            condition="natural",
            prompt_ids={"L4_13"},
        )[0]
        self.assertEqual(
            stable_batch_seed(42, request, 0, 8),
            stable_batch_seed(42, request, 0, 8),
        )
        self.assertNotEqual(
            stable_batch_seed(42, request, 0, 8),
            stable_batch_seed(42, request, 8, 16),
        )

    def test_resume_loads_and_validates_completed_sample(self) -> None:
        request = build_requests(
            self.prompt_set,
            condition="natural",
            prompt_ids={"L4_13"},
        )[0]
        intended = expected_samples([request], 2)
        required_config = {
            "backend": "pytorch_transformers",
            "model": "test/model",
            "samples_per_prompt": 2,
        }
        record = {
            "sample_id": "L4_13.natural.s01",
            "prompt_id": request.prompt_id,
            "condition": request.condition,
            "sample_number": 1,
            "prompt": request.prompt,
            "generation_config": {**required_config, "batch_size": 8},
        }
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "partial.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            completed, batch_sizes = load_completed_samples(
                path,
                expected=intended,
                required_config=required_config,
            )
        self.assertEqual(completed, {"L4_13.natural.s01"})
        self.assertEqual(batch_sizes, {8})

    def test_resume_requires_explicit_existing_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an explicit"):
            output_path(None, resume=True)
        with TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.jsonl"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                output_path(missing, resume=True)

    def test_tokens_with_reasoning_are_split(self) -> None:
        parsed = parse_tokens(
            [1, 2, 3, 4],
            tokenizer=FakeTokenizer(),
            reasoning_end_marker="</think>",
            eos_ids={4},
        )
        self.assertEqual(parsed["reasoning"], "reasoning")
        self.assertEqual(parsed["response"], "final")
        self.assertTrue(parsed["reasoning_complete"])
        self.assertEqual(parsed["finish_reason"], "stop")

    def test_tokens_without_reasoning_are_kept_as_response(self) -> None:
        parsed = parse_tokens(
            [3, 4],
            tokenizer=FakeTokenizer(),
            reasoning_end_marker="</think>",
            eos_ids={4},
        )
        self.assertIsNone(parsed["reasoning"])
        self.assertEqual(parsed["response"], "final")
        self.assertFalse(parsed["reasoning_complete"])


if __name__ == "__main__":
    unittest.main()
