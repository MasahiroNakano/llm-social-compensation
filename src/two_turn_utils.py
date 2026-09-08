"""Shared validation and message-building helpers for two-turn experiments."""

from __future__ import annotations

from typing import Any


def validate_source_record(record: dict[str, Any]) -> None:
    """Validate the saved fields needed to reconstruct a first turn."""

    for field in ("prompt", "response"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Source row must contain a non-empty {field!r} string.")
    if not isinstance(record.get("generation_config"), dict):
        raise ValueError("Source row must contain a 'generation_config' object.")


def build_messages(
    *,
    source_record: dict[str, Any],
    followup_prompt: str,
    system_prompt: str,
) -> list[dict[str, str]]:
    """Construct system, saved user/assistant, and new user messages."""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": source_record["prompt"].strip()},
        {"role": "assistant", "content": source_record["response"].strip()},
        {"role": "user", "content": followup_prompt.strip()},
    ]


def validate_generation_settings(
    *,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    reasoning_end_marker: str,
) -> None:
    """Validate sampling settings shared by two-turn runners."""

    if max_new_tokens <= 0:
        raise ValueError("--max-new-tokens must be greater than 0.")
    if temperature <= 0:
        raise ValueError("--temperature must be greater than 0 for sampling.")
    if not 0 < top_p <= 1:
        raise ValueError("--top-p must be in (0, 1].")
    if not reasoning_end_marker:
        raise ValueError("--reasoning-end-marker must not be empty.")
