"""Fast tests for vLLM batch serialization; no GPU or model is required."""

from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from batch_qwen_reasoning_vllm import (
    BatchAnswer,
    RunMetadata,
    completion_to_answer,
    write_outputs,
)


class FakeTokenizer:
    unk_token_id = 0

    def convert_tokens_to_ids(self, token: str) -> int:
        return 2 if token == "</think>" else self.unk_token_id

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        pieces = {1: "reasoning", 2: "</think>", 3: "final", 4: "<eos>"}
        special_ids = {2, 4} if skip_special_tokens else set()
        return "".join(pieces[token] for token in token_ids if token not in special_ids)


class BatchOutputTests(unittest.TestCase):
    def test_completion_is_split_without_importing_vllm(self) -> None:
        completion = SimpleNamespace(
            token_ids=[1, 2, 3, 4],
            finish_reason="stop",
            stop_reason=None,
        )

        result = completion_to_answer(
            completion,
            answer_number=1,
            prompt="question",
            tokenizer=FakeTokenizer(),
            reasoning_end_marker="</think>",
        )

        self.assertEqual(result.reasoning, "reasoning")
        self.assertEqual(result.answer, "final")
        self.assertTrue(result.reasoning_complete)
        self.assertEqual(result.raw_response, "reasoning</think>final<eos>")
        self.assertEqual(result.generated_tokens, 4)

    def test_writes_one_csv_and_one_markdown_file(self) -> None:
        answers = [
            BatchAnswer(
                answer_number=1,
                prompt="question",
                reasoning="work",
                answer="answer one",
                reasoning_complete=True,
                raw_response="work</think>answer one",
                finish_reason="stop",
                stop_reason="",
                generated_tokens=5,
            ),
            BatchAnswer(
                answer_number=2,
                prompt="question",
                reasoning="more work",
                answer="answer two",
                reasoning_complete=True,
                raw_response="more work</think>answer two",
                finish_reason="stop",
                stop_reason="",
                generated_tokens=6,
            ),
        ]
        metadata = RunMetadata(
            model="test/model",
            prompt="question",
            num_answers=2,
            max_new_tokens=100,
            temperature=0.6,
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            seed=0,
            prompt_tokens=3,
            total_generated_tokens=11,
            generation_seconds=1.0,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            csv_path = root / "results.csv"
            markdown_path = root / "results.md"
            write_outputs(answers, metadata, csv_path, markdown_path)

            self.assertEqual(
                sorted(path.suffix for path in root.iterdir()), [".csv", ".md"]
            )
            with csv_path.open(encoding="utf-8", newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["answer"], "answer one")
            self.assertEqual(rows[1]["reasoning"], "more work")

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Answer 1", markdown)
            self.assertIn("answer one", markdown)
            self.assertIn("## Answer 2", markdown)
            self.assertIn("answer two", markdown)


if __name__ == "__main__":
    unittest.main()
