"""Fast configuration tests for the three resumable experiment phases."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.phase1_single_turn import DEFAULT_OUTPUT as PHASE1_OUTPUT
from scripts.phase1_single_turn import DEFAULT_PROMPTS, parse_args as parse_phase1_args
from scripts.phase2_two_turns import (
    DEFAULT_SOURCE_JSONL,
    ORDERED_PAIRS,
    PROMPT_IDS,
    namespace_for_pair,
    pair_output_path,
    parse_args as parse_phase2_args,
    run as run_phase2,
    selected_pairs,
)
from scripts.phase2_two_turns_prompt_control import (
    DEFAULT_OUTPUT_DIR as PHASE2_CONTROL_OUTPUT_DIR,
    Q2_PREFIX,
    parse_args as parse_phase2_control_args,
)
from scripts.phase3_experiment import (
    DEFAULT_FOLLOWUP_PROMPT_IDS as PHASE3_FOLLOWUPS,
    DEFAULT_OUTPUT as PHASE3_OUTPUT,
    DEFAULT_PROMPTS as PHASE3_PROMPTS,
    FIRST_PROMPT_ID as PHASE3_FIRST_PROMPT,
    build_experiment as build_phase3_experiment,
    messages_for_cell as phase3_messages,
    parse_args as parse_phase3_args,
)
from src.batch_qwen import load_prompt_set
from src.batch_qwen import repair_incomplete_jsonl_tail


class PhaseOneTests(unittest.TestCase):
    def test_phase_prompt_file_contains_exactly_the_four_new_ids(self) -> None:
        prompt_set = load_prompt_set(DEFAULT_PROMPTS)
        prompt_ids = {prompt["id"] for prompt in prompt_set["prompts"]}
        self.assertEqual(prompt_ids, set(PROMPT_IDS))

    def test_temperature_output_and_prompt_defaults(self) -> None:
        args = parse_phase1_args([])
        self.assertEqual(args.temperature, 1.0)
        self.assertEqual(args.prompts, DEFAULT_PROMPTS)
        self.assertEqual(args.output, PHASE1_OUTPUT)

    def test_resume_has_a_deterministic_default_output(self) -> None:
        args = parse_phase1_args(["--resume"])
        self.assertTrue(args.resume)
        self.assertEqual(args.output, PHASE1_OUTPUT)

    def test_resume_repair_discards_only_an_incomplete_tail(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "partial.jsonl"
            complete_line = b'{"sample_id":"complete"}\n'
            path.write_bytes(complete_line + b'{"sample_id":"interrupted"')
            message = repair_incomplete_jsonl_tail(path)
            repaired = path.read_bytes()
        self.assertIn("Discarded", message or "")
        self.assertEqual(repaired, complete_line)

    def test_resume_repair_keeps_a_complete_unterminated_record(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "complete.jsonl"
            path.write_bytes(b'{"sample_id":"complete"}')
            message = repair_incomplete_jsonl_tail(path)
            repaired = path.read_bytes()
        self.assertIn("newline", message or "")
        self.assertEqual(repaired, b'{"sample_id":"complete"}\n')


class PhaseTwoTests(unittest.TestCase):
    def test_all_twelve_directed_pairs_are_present(self) -> None:
        self.assertEqual(len(ORDERED_PAIRS), 12)
        self.assertEqual(len(set(ORDERED_PAIRS)), 12)
        self.assertTrue(all(source != followup for source, followup in ORDERED_PAIRS))
        self.assertEqual(
            set(ORDERED_PAIRS),
            {
                (source, followup)
                for source in PROMPT_IDS
                for followup in PROMPT_IDS
                if source != followup
            },
        )

    def test_phase_two_defaults_to_phase_one_output_and_temperature_one(self) -> None:
        args = parse_phase2_args([])
        self.assertEqual(args.source_jsonl, DEFAULT_SOURCE_JSONL)
        self.assertEqual(DEFAULT_SOURCE_JSONL, PHASE1_OUTPUT)
        self.assertEqual(args.temperature, 1.0)
        self.assertEqual(selected_pairs(args), ORDERED_PAIRS)

    def test_pair_selector_deduplicates_without_reordering(self) -> None:
        args = parse_phase2_args(
            ["--pair", "P4:P1", "--pair", "P1:P2", "--pair", "P4:P1"]
        )
        self.assertEqual(selected_pairs(args), (("P4", "P1"), ("P1", "P2")))

    def test_pair_namespace_points_at_deterministic_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            args = parse_phase2_args(
                [
                    "--output-dir",
                    temporary_directory,
                    "--source-jsonl",
                    "phase1.jsonl",
                ]
            )
            pair_args = namespace_for_pair(args, ("P1", "P3"), resume=True)
            expected = pair_output_path(Path(temporary_directory), "P1", "P3")
        self.assertEqual(pair_args.source_prompt_id, "P1")
        self.assertEqual(pair_args.followup_prompt_id, "P3")
        self.assertEqual(pair_args.output, expected)
        self.assertEqual(pair_args.temperature, 1.0)
        self.assertTrue(pair_args.resume)

    def test_phase_two_prompt_control_has_separate_output_and_q2_prefix(self) -> None:
        args = parse_phase2_control_args([])
        pair_args = namespace_for_pair(args, ("P1", "P3"), resume=False)
        self.assertEqual(args.output_dir, PHASE2_CONTROL_OUTPUT_DIR)
        self.assertEqual(pair_args.followup_prefix, Q2_PREFIX)
        self.assertNotEqual(args.output_dir, parse_phase2_args([]).output_dir)

    def test_sweep_resume_continues_partial_and_starts_missing_pairs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            partial_output = pair_output_path(output_dir, "P1", "P2")
            partial_output.write_text("one completed record\n", encoding="utf-8")
            args = parse_phase2_args(
                [
                    "--output-dir",
                    str(output_dir),
                    "--pair",
                    "P1:P2",
                    "--pair",
                    "P2:P1",
                    "--resume",
                ]
            )
            shared_runtime = (object(), object(), object())
            with (
                patch("scripts.phase2_two_turns.run_pair", return_value=0) as pair_runner,
                patch(
                    "scripts.phase2_two_turns.load_runtime",
                    return_value=shared_runtime,
                ) as runtime_loader,
            ):
                result = run_phase2(args)

        self.assertEqual(result, 0)
        runtime_loader.assert_called_once()
        actual_calls = [
            call for call in pair_runner.call_args_list if not call.args[0].dry_run
        ]
        self.assertEqual(len(actual_calls), 2)
        actual_by_pair = {
            (call.args[0].source_prompt_id, call.args[0].followup_prompt_id): call
            for call in actual_calls
        }
        self.assertTrue(actual_by_pair[("P1", "P2")].args[0].resume)
        self.assertFalse(actual_by_pair[("P2", "P1")].args[0].resume)
        self.assertTrue(
            all(call.kwargs["runtime"] is shared_runtime for call in actual_calls)
        )


class PhaseThreeTests(unittest.TestCase):
    def test_defaults_define_the_single_matched_a1_pair(self) -> None:
        args = parse_phase3_args([])
        self.assertEqual(args.temperature, 1.0)
        self.assertEqual(args.prompts, PHASE3_PROMPTS)
        self.assertEqual(args.output, PHASE3_OUTPUT)
        self.assertEqual(PHASE3_FIRST_PROMPT, "P2")
        self.assertEqual(PHASE3_FOLLOWUPS, ("P3", "P4"))

    def test_each_phase_three_conversation_contains_exactly_one_a1(self) -> None:
        prompt_set = load_prompt_set(PHASE3_PROMPTS)
        first_request, cells = build_phase3_experiment(
            prompt_set,
            followup_prompt_ids=PHASE3_FOLLOWUPS,
            verdict_conditions=("positive", "negative"),
        )
        self.assertEqual(len(cells), 4)
        for cell in cells:
            messages = phase3_messages(
                cell,
                first_request=first_request,
                system_prompt=prompt_set["system_prompt"],
            )
            self.assertEqual(
                [message["role"] for message in messages],
                ["system", "user", "assistant", "user"],
            )
            self.assertEqual(messages[2]["content"], cell.first_answer)


if __name__ == "__main__":
    unittest.main()
