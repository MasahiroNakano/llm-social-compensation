#!/usr/bin/env python3
"""Convert Qwen batch JSONL results into human-readable Markdown."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence


REQUIRED_FIELDS = {
    "sample_id",
    "prompt_id",
    "level",
    "level_label",
    "title",
    "condition",
    "prompt",
    "sample_number",
    "response",
    "reasoning",
    "reasoning_complete",
    "raw_response",
    "finish_reason",
    "reasoning_tokens",
    "response_tokens",
    "generated_tokens",
    "generation_config",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="One or more JSONL files produced by batch_qwen.py.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path (only valid with one input file).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write Markdown files to this directory instead of beside each input.",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw_response even when it duplicates reasoning and response.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing Markdown files.",
    )
    return parser.parse_args(argv)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sample_ids: set[str] = set()

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError(f"Input file does not exist: {path}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: each row must be a JSON object")

        missing = REQUIRED_FIELDS - record.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"{path}:{line_number}: missing fields: {names}")
        if not isinstance(record["generation_config"], dict):
            raise ValueError(
                f"{path}:{line_number}: generation_config must be an object"
            )

        sample_id = record["sample_id"]
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"{path}:{line_number}: sample_id must be a string")
        if sample_id in sample_ids:
            raise ValueError(f"{path}:{line_number}: duplicate sample_id {sample_id!r}")
        sample_ids.add(sample_id)
        records.append(record)

    if not records:
        raise ValueError(f"Input file contains no JSON records: {path}")
    return records


def quote_block(value: Any) -> str:
    """Render arbitrary multiline text as a Markdown block quote."""

    return "\n".join(">" if line == "" else f"> {line}" for line in str(value).split("\n"))


def inline_code(value: Any) -> str:
    text = str(value)
    fence = "``" if "`" in text else "`"
    return f"{fence}{text}{fence}"


def table_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def config_signature(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def raw_is_redundant(record: dict[str, Any]) -> bool:
    expected = (
        str(record.get("reasoning") or "")
        + "\n\n"
        + str(record["response"])
        + "<|im_end|>"
    )
    return record.get("raw_response") == expected


def unique_values(records: list[dict[str, Any]], key: str) -> list[Any]:
    values: list[Any] = []
    seen: set[str] = set()
    for record in records:
        value = record[key]
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if signature not in seen:
            seen.add(signature)
            values.append(value)
    return values


def render_config_table(config: dict[str, Any]) -> list[str]:
    lines = ["| Setting | Value |", "|---|---|"]
    for key, value in config.items():
        label = key.replace("_", " ").capitalize()
        lines.append(f"| {label} | {table_cell(value)} |")
    return lines


def render_markdown(
    records: list[dict[str, Any]],
    source_name: str,
    *,
    include_raw: bool = False,
) -> str:
    configs: list[dict[str, Any]] = []
    config_numbers: dict[str, int] = {}
    for record in records:
        config = record["generation_config"]
        signature = config_signature(config)
        if signature not in config_numbers:
            configs.append(config)
            config_numbers[signature] = len(configs)

    prompt_ids = unique_values(records, "prompt_id")
    conditions = unique_values(records, "condition")
    models = []
    generated_at = []
    configured_sample_counts = []
    for config in configs:
        for key, destination in (
            ("model", models),
            ("generated_at", generated_at),
            ("samples_per_prompt", configured_sample_counts),
        ):
            if key in config and config[key] not in destination:
                destination.append(config[key])

    lines = [
        "# Criticism baseline results",
        "",
        f"Human-readable rendering of {inline_code(source_name)}.",
        "",
        f"- Records: **{len(records)}**",
        f"- Prompts: **{len(prompt_ids)}**",
    ]
    if configured_sample_counts:
        counts = ", ".join(str(value) for value in configured_sample_counts)
        lines.append(f"- Configured samples per prompt: **{counts}**")
    if models:
        lines.append("- Model(s): " + ", ".join(inline_code(value) for value in models))
    if generated_at:
        lines.append(
            "- Generated: " + ", ".join(inline_code(value) for value in generated_at)
        )
    lines.append(
        "- Condition(s): " + ", ".join(inline_code(value) for value in conditions)
    )
    lines.append("")

    redundant_count = sum(raw_is_redundant(record) for record in records)
    if not include_raw and redundant_count:
        if redundant_count == len(records):
            lines.extend(
                [
                    "> `raw_response` is not repeated below: every record contains "
                    "the same reasoning and response in parsed form, plus only "
                    "separators and the end token. All non-redundant content is "
                    "preserved.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"> `raw_response` is omitted for the {redundant_count} records "
                    "where it duplicates the parsed reasoning and response. "
                    "Non-redundant raw responses are included.",
                    "",
                ]
            )

    config_heading = "configuration" if len(configs) == 1 else "configurations"
    lines.extend([f"## Generation {config_heading}", ""])
    for index, config in enumerate(configs, start=1):
        if len(configs) > 1:
            lines.extend([f"### Configuration {index}", ""])
        lines.extend(render_config_table(config))
        lines.append("")

    groups: OrderedDict[tuple[Any, Any], list[dict[str, Any]]] = OrderedDict()
    for record in records:
        key = (record["prompt_id"], record["condition"])
        groups.setdefault(key, []).append(record)

    show_condition_in_heading = len(conditions) > 1
    for (prompt_id, condition), group in groups.items():
        first = group[0]
        heading = f"## {inline_code(prompt_id)} — {first['title']}"
        if show_condition_in_heading:
            heading += f" — {condition}"
        lines.extend(
            [
                heading,
                "",
                f"- Level: **{first['level']}** ({inline_code(first['level_label'])})",
                f"- Condition: {inline_code(condition)}",
                f"- Samples: **{len(group)}**",
                "",
                "### Prompt",
                "",
                quote_block(first["prompt"]),
                "",
                "### Samples",
                "",
            ]
        )

        for group_index, record in enumerate(group):
            if group_index:
                lines.extend(["---", ""])
            lines.extend(
                [
                    f"#### Sample {record['sample_number']} — "
                    f"{inline_code(record['sample_id'])}",
                    "",
                    "| Finish reason | Reasoning complete | Reasoning tokens | "
                    "Response tokens | Generated tokens |",
                    "|---|---:|---:|---:|---:|",
                    f"| {inline_code(record['finish_reason'])} | "
                    f"{'yes' if record['reasoning_complete'] else 'no'} | "
                    f"{record['reasoning_tokens']} | {record['response_tokens']} | "
                    f"{record['generated_tokens']} |",
                    "",
                ]
            )
            if len(configs) > 1:
                number = config_numbers[config_signature(record["generation_config"])]
                lines.extend([f"Generation configuration: **{number}**", ""])

            lines.extend(["##### Response", "", quote_block(record["response"]), ""])

            reasoning = record.get("reasoning") or ""
            if reasoning:
                lines.extend(
                    [
                        "<details>",
                        f"<summary>Reasoning ({record['reasoning_tokens']} tokens)</summary>",
                        "",
                        quote_block(reasoning),
                        "",
                        "</details>",
                        "",
                    ]
                )
            else:
                lines.extend(["_No separate reasoning was parsed._", ""])

            if include_raw or not raw_is_redundant(record):
                lines.extend(
                    [
                        "<details>",
                        "<summary>Raw response</summary>",
                        "",
                        quote_block(record["raw_response"]),
                        "",
                        "</details>",
                        "",
                    ]
                )

    return "\n".join(lines).rstrip() + "\n"


def output_path_for(
    input_path: Path,
    *,
    output: Path | None,
    output_dir: Path | None,
) -> Path:
    if output is not None:
        return output
    filename = input_path.with_suffix(".md").name
    return output_dir / filename if output_dir is not None else input_path.with_suffix(".md")


def write_text_atomic(path: Path, text: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ValueError(f"Output already exists (use --force to replace it): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output is not None and len(args.inputs) != 1:
        raise SystemExit("--output can only be used with one input file")
    if args.output is not None and args.output_dir is not None:
        raise SystemExit("--output and --output-dir cannot be used together")

    jobs = [
        (
            input_path,
            output_path_for(
                input_path,
                output=args.output,
                output_dir=args.output_dir,
            ),
        )
        for input_path in args.inputs
    ]
    resolved_outputs: set[Path] = set()
    for input_path, output_path in jobs:
        resolved_input = input_path.resolve()
        resolved_output = output_path.resolve()
        if resolved_input == resolved_output:
            raise SystemExit(f"Input and output paths must differ: {input_path}")
        if resolved_output in resolved_outputs:
            raise SystemExit(f"Multiple inputs map to the same output: {output_path}")
        resolved_outputs.add(resolved_output)

    try:
        for input_path, output_path in jobs:
            records = load_jsonl(input_path)
            markdown = render_markdown(
                records,
                input_path.name,
                include_raw=args.include_raw,
            )
            write_text_atomic(output_path, markdown, overwrite=args.force)
            print(f"Wrote {len(records)} records to {output_path}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
