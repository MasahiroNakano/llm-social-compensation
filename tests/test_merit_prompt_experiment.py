"""Configuration tests for the isolated merit-system-prompt experiment."""

from __future__ import annotations

import unittest

from scripts.phase1_single_turn import DEFAULT_OUTPUT as BASELINE_PHASE1_OUTPUT
from scripts.phase1_single_turn_merit_prompt import (
    DEFAULT_OUTPUT as MERIT_PHASE1_OUTPUT,
    DEFAULT_SYSTEM_PROMPT,
    parse_args as parse_merit_phase1_args,
)
from scripts.phase2_two_turns import DEFAULT_OUTPUT_DIR as BASELINE_PHASE2_OUTPUT_DIR
from scripts.phase2_two_turns_merit_prompt import (
    DEFAULT_OUTPUT_DIR as MERIT_PHASE2_OUTPUT_DIR,
    DEFAULT_SOURCE_JSONL,
    namespace_for_pair,
    parse_args as parse_merit_phase2_args,
)


class MeritPromptExperimentTests(unittest.TestCase):
    def test_intervention_prompt_matches_the_proposed_instruction(self) -> None:
        self.assertEqual(
            DEFAULT_SYSTEM_PROMPT,
            "You are a helpful research assistant who gives answers based purely "
            "on merit and does not compensate for the previous negative assessment.",
        )

    def test_phase_one_defaults_to_intervention_and_separate_output(self) -> None:
        args = parse_merit_phase1_args([])

        self.assertEqual(args.system_prompt, DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(args.output, MERIT_PHASE1_OUTPUT)
        self.assertNotEqual(args.output, BASELINE_PHASE1_OUTPUT)

    def test_phase_one_system_prompt_remains_overridable(self) -> None:
        args = parse_merit_phase1_args(["--system-prompt", "alternate prompt"])

        self.assertEqual(args.system_prompt, "alternate prompt")

    def test_phase_two_uses_intervention_phase_one_and_separate_output(self) -> None:
        args = parse_merit_phase2_args([])

        self.assertEqual(args.system_prompt, DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(args.source_jsonl, MERIT_PHASE1_OUTPUT)
        self.assertEqual(args.source_jsonl, DEFAULT_SOURCE_JSONL)
        self.assertEqual(args.output_dir, MERIT_PHASE2_OUTPUT_DIR)
        self.assertNotEqual(args.output_dir, BASELINE_PHASE2_OUTPUT_DIR)

    def test_phase_two_passes_override_to_each_pair(self) -> None:
        args = parse_merit_phase2_args(
            ["--system-prompt", "alternate prompt", "--pair", "P1:P2"]
        )

        pair_args = namespace_for_pair(args, ("P1", "P2"), resume=False)

        self.assertEqual(pair_args.system_prompt, "alternate prompt")


if __name__ == "__main__":
    unittest.main()
