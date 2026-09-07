"""Shared Qwen model, cache, device, and reasoning-output utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MODEL = "Qwen/Qwen3.5-4B"
DEFAULT_PROMPT = (
    "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
    "ball. How much does the ball cost?"
)
DEFAULT_REASONING_END_MARKER = "</think>"


def configure_cache(cache_dir: Path | None) -> Path:
    """Choose a persistent Hugging Face cache before importing Transformers."""

    if cache_dir is not None:
        chosen = cache_dir.expanduser().resolve()
        os.environ["HF_HOME"] = str(chosen)
    elif os.environ.get("HF_HOME"):
        chosen = Path(os.environ["HF_HOME"]).expanduser()
    else:
        runpod_volume = os.environ.get("RUNPOD_VOLUME_PATH")
        candidates = [Path(runpod_volume)] if runpod_volume else []
        candidates.append(Path("/workspace"))
        writable_root = next(
            (path for path in candidates if path.is_dir() and os.access(path, os.W_OK)),
            Path.home(),
        )
        chosen = writable_root / ".cache" / "huggingface"
        os.environ["HF_HOME"] = str(chosen)

    chosen.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_CACHE", str(chosen / "hub"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return chosen


def choose_dtype(torch_module: Any) -> Any:
    """Choose BF16 when supported and otherwise use FP16."""

    if torch_module.cuda.is_bf16_supported():
        return torch_module.bfloat16
    return torch_module.float16


def dtype_load_kwargs(transformers_version: str, dtype: Any) -> dict[str, Any]:
    """Use the non-deprecated dtype keyword when the installed version supports it."""

    numeric_parts: list[int] = []
    for part in transformers_version.split(".")[:2]:
        digits = "".join(character for character in part if character.isdigit())
        numeric_parts.append(int(digits) if digits else 0)
    version = tuple((numeric_parts + [0, 0])[:2])
    return {"dtype": dtype} if version >= (4, 56) else {"torch_dtype": dtype}


def input_device_for(model: Any) -> Any:
    """Return the device that should receive tokenized model inputs."""

    embeddings = model.get_input_embeddings()
    if embeddings is not None and hasattr(embeddings, "weight"):
        return embeddings.weight.device
    return next(model.parameters()).device


def marker_token_ids(tokenizer: Any, marker: str) -> list[int]:
    """Resolve a reasoning-end marker to one or more token IDs."""

    marker_id = tokenizer.convert_tokens_to_ids(marker)
    if marker_id is not None and marker_id != tokenizer.unk_token_id:
        return [int(marker_id)]

    encoded = tokenizer.encode(marker, add_special_tokens=False)
    if not encoded:
        raise RuntimeError(f"Tokenizer could not encode reasoning marker {marker!r}.")
    return [int(token_id) for token_id in encoded]


def find_last_subsequence(tokens: Sequence[int], marker: Sequence[int]) -> int | None:
    """Return the start of the last marker occurrence, or None when absent."""

    if not marker or len(marker) > len(tokens):
        return None
    for start in range(len(tokens) - len(marker), -1, -1):
        if list(tokens[start : start + len(marker)]) == list(marker):
            return start
    return None


def split_reasoning_output(
    output_ids: Sequence[int], tokenizer: Any, marker: str
) -> tuple[str, str, bool, int, int]:
    """Split generated tokens into reasoning and final-answer text."""

    end_ids = marker_token_ids(tokenizer, marker)
    marker_start = find_last_subsequence(output_ids, end_ids)
    if marker_start is None:
        unparsed = tokenizer.decode(output_ids, skip_special_tokens=True).strip()
        return unparsed, "", False, len(output_ids), 0

    answer_start = marker_start + len(end_ids)
    reasoning = tokenizer.decode(
        output_ids[:answer_start], skip_special_tokens=True
    ).strip()
    answer = tokenizer.decode(
        output_ids[answer_start:], skip_special_tokens=True
    ).strip()
    return reasoning, answer, True, marker_start, len(output_ids) - answer_start
