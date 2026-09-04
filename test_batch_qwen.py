"""Fast tests for the criticism-baseline batch runner; no GPU is required."""

from __future__ import annotations

import unittest

from batch_qwen import (
    DEFAULT_PROMPTS,
    batch_ranges,
    build_requests,
    load_prompt_set,
    parse_tokens,
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
