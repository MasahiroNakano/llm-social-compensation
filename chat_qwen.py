#!/usr/bin/env python3
"""Interactive Qwen chat with reusable answer and reasoning output.

The reasoning trace exposed here is text emitted by the model. It should not be
treated as a complete or faithful account of the model's internal computation.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any, Sequence, TextIO


DEFAULT_MODEL = "Qwen/Qwen3.5-4B"
DEFAULT_SYSTEM_PROMPT = "You are a helpful research assistant."
DEFAULT_REASONING_END_MARKER = "</think>"


@dataclass(frozen=True)
class LLMOutput:
    """A single generation that can be printed repeatedly without rerunning it."""

    prompt: str
    response: str
    reasoning: str | None
    raw_response: str
    reasoning_complete: bool
    generated_tokens: int
    generation_seconds: float


@dataclass(frozen=True)
class _Runtime:
    torch: Any
    tokenizer: Any
    model: Any
    input_device: Any


class _Tee:
    """Write console output to the terminal and the run transcript."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(stream.isatty() for stream in self.streams)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--prompt",
        help="Generate one response and exit. If omitted, start an interactive chat.",
    )
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help="System prompt used for the conversation.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=8_192,
        help="Maximum generated tokens per response, including reasoning (default: 8192).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.6,
        help="Sampling temperature (default: 0.6).",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.95,
        help="Nucleus-sampling cutoff (default: 0.95).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Top-k sampling cutoff (default: 20).",
    )
    parser.add_argument(
        "--min-p",
        type=float,
        default=0.0,
        help="Minimum-token-probability cutoff (default: 0.0).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed used for each response (default: 0).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Hugging Face cache root; defaults to persistent RunPod storage.",
    )
    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="Print the model-emitted reasoning trace before each response.",
    )
    parser.add_argument(
        "--show-stats",
        action="store_true",
        help="Print token count, generation time, and throughput.",
    )
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Allow very slow CPU inference when CUDA is unavailable.",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help=(
            "Transcript path. By default, create one timestamped Markdown file "
            "under outputs/."
        ),
    )
    return parser.parse_args()


def validate_generation_args(
    *,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    min_p: float,
) -> None:
    if max_new_tokens <= 0:
        raise ValueError("max_new_tokens must be greater than 0")
    if temperature <= 0:
        raise ValueError("temperature must be greater than 0")
    if not 0 < top_p <= 1:
        raise ValueError("top_p must be in (0, 1]")
    if top_k <= 0:
        raise ValueError("top_k must be greater than 0")
    if not 0 <= min_p <= 1:
        raise ValueError("min_p must be in [0, 1]")


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
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    return chosen


def _transcript_path(requested_path: Path | None) -> Path:
    if requested_path is not None:
        return requested_path.expanduser().resolve()
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
    return (Path.cwd() / "outputs" / f"chat_qwen_{timestamp}.md").resolve()


def _write_transcript_header(
    transcript: TextIO, path: Path, args: argparse.Namespace
) -> None:
    started = datetime.now().astimezone()
    print("# Qwen chat transcript", file=transcript)
    print(file=transcript)
    print(f"- Started: {started.isoformat(timespec='seconds')}", file=transcript)
    print(f"- Model: `{args.model}`", file=transcript)
    print(
        f"- Console reasoning initially shown: `{args.show_reasoning}`",
        file=transcript,
    )
    print(f"- Console statistics shown: `{args.show_stats}`", file=transcript)
    print(f"- Maximum new tokens: `{args.max_new_tokens}`", file=transcript)
    print(f"- Transcript: `{path}`", file=transcript)
    print("\n---\n", file=transcript)
    transcript.flush()


def _write_system_message(transcript: TextIO, message: str) -> None:
    rendered = html.escape(message).replace("\n", "<br>\n")
    print(
        f'<p align="center"><em>⚙️ System: {rendered}</em></p>\n',
        file=transcript,
    )
    transcript.flush()


def _write_user_message(transcript: TextIO, message: str) -> None:
    rendered = html.escape(message).replace("\n", "<br>\n")
    print('<table width="100%">', file=transcript)
    print("<tr>", file=transcript)
    print('<td width="40%"></td>', file=transcript)
    print('<td width="60%" align="right">', file=transcript)
    print(f"<strong>👤 User</strong><br><br>\n{rendered}", file=transcript)
    print("</td>", file=transcript)
    print("</tr>", file=transcript)
    print("</table>\n", file=transcript)
    transcript.flush()


def _write_assistant_message(
    transcript: TextIO, output: LLMOutput, *, reasoning_open: bool
) -> None:
    if output.reasoning_complete:
        response = output.response or "[The model emitted no final response.]"
    else:
        response = (
            "[No final response could be separated. The reasoning was probably "
            "truncated; retry with a larger max_new_tokens value.]"
        )
    rendered_response = html.escape(response).replace("\n", "<br>\n")
    rendered_reasoning = html.escape(
        output.reasoning or "[No reasoning text was generated.]"
    )
    details_attribute = " open" if reasoning_open else ""
    throughput = (
        output.generated_tokens / output.generation_seconds
        if output.generation_seconds > 0
        else 0.0
    )

    print('<table width="100%">', file=transcript)
    print("<tr>", file=transcript)
    print('<td width="70%" align="left">', file=transcript)
    print("<strong>🤖 Assistant</strong><br><br>", file=transcript)
    print(rendered_response, file=transcript)
    print("<br><br>", file=transcript)
    print(f"<details{details_attribute}>", file=transcript)
    print("<summary>🧠 Model-emitted reasoning</summary>", file=transcript)
    print(f"<pre>{rendered_reasoning}</pre>", file=transcript)
    print("</details>", file=transcript)
    print("<br>", file=transcript)
    print(
        "<sub>"
        f"{output.generated_tokens} generated tokens · "
        f"{output.generation_seconds:.2f} s · {throughput:.2f} tokens/s"
        "</sub>",
        file=transcript,
    )
    print("</td>", file=transcript)
    print('<td width="30%"></td>', file=transcript)
    print("</tr>", file=transcript)
    print("</table>\n", file=transcript)
    transcript.flush()


def _write_console_log(transcript: TextIO, console_output: str) -> None:
    """Preserve every emitted console line in a collapsed transcript appendix."""

    print("\n---\n", file=transcript)
    print("<details>", file=transcript)
    print("<summary>Complete console log</summary>", file=transcript)
    print(f"<pre>{html.escape(console_output)}</pre>", file=transcript)
    print("</details>", file=transcript)
    transcript.flush()


def _dtype_load_kwargs(transformers_version: str, dtype: Any) -> dict[str, Any]:
    """Use the dtype keyword supported by the installed Transformers version."""

    numeric_parts: list[int] = []
    for part in transformers_version.split(".")[:2]:
        digits = "".join(character for character in part if character.isdigit())
        numeric_parts.append(int(digits) if digits else 0)
    version = tuple((numeric_parts + [0, 0])[:2])
    return {"dtype": dtype} if version >= (4, 56) else {"torch_dtype": dtype}


def _input_device_for(model: Any) -> Any:
    embeddings = model.get_input_embeddings()
    if embeddings is not None and hasattr(embeddings, "weight"):
        return embeddings.weight.device
    return next(model.parameters()).device


@lru_cache(maxsize=2)
def _load_runtime(model_name: str, cache_root: str, allow_cpu: bool) -> _Runtime:
    """Load a model once per Python process and reuse it for later prompts."""

    try:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run ./setup.sh first.") from exc

    using_cuda = torch.cuda.is_available()
    if not using_cuda and not allow_cpu:
        raise RuntimeError(
            "CUDA is unavailable. Use a RunPod PyTorch GPU pod or pass allow_cpu=True."
        )

    dtype = (
        torch.bfloat16
        if using_cuda and torch.cuda.is_bf16_supported()
        else torch.float16 if using_cuda else torch.float32
    )
    device_description = (
        torch.cuda.get_device_properties(torch.cuda.current_device()).name
        if using_cuda
        else "CPU"
    )
    print(f"Loading {model_name}", file=sys.stderr)
    print(
        f"Device: {device_description}; precision: {str(dtype).removeprefix('torch.')}",
        file=sys.stderr,
    )
    print(f"HF cache: {cache_root}", file=sys.stderr)

    hub_cache = str(Path(cache_root) / "hub")
    tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=hub_cache)
    load_kwargs: dict[str, Any] = {
        "cache_dir": hub_cache,
        "low_cpu_mem_usage": True,
        **_dtype_load_kwargs(transformers.__version__, dtype),
    }
    if using_cuda:
        load_kwargs["device_map"] = "auto"

    model = AutoModelForCausalLM.from_pretrained(model_name, **load_kwargs)
    if not using_cuda:
        model.to("cpu")
    model.eval()
    return _Runtime(torch, tokenizer, model, _input_device_for(model))


def _marker_token_ids(tokenizer: Any, marker: str) -> list[int]:
    marker_id = tokenizer.convert_tokens_to_ids(marker)
    if marker_id is not None and marker_id != tokenizer.unk_token_id:
        return [int(marker_id)]

    encoded = tokenizer.encode(marker, add_special_tokens=False)
    if not encoded:
        raise RuntimeError(f"Tokenizer could not encode reasoning marker {marker!r}.")
    return [int(token_id) for token_id in encoded]


def _find_last_subsequence(tokens: Sequence[int], marker: Sequence[int]) -> int | None:
    if not marker or len(marker) > len(tokens):
        return None
    for start in range(len(tokens) - len(marker), -1, -1):
        if list(tokens[start : start + len(marker)]) == list(marker):
            return start
    return None


def _split_reasoning_output(
    output_ids: Sequence[int], tokenizer: Any, marker: str
) -> tuple[str | None, str, bool, str]:
    """Split token IDs before decoding so special-token removal keeps the boundary."""

    marker_ids = _marker_token_ids(tokenizer, marker)
    marker_start = _find_last_subsequence(output_ids, marker_ids)
    raw_response = tokenizer.decode(output_ids, skip_special_tokens=False).strip()
    if marker_start is None:
        reasoning = tokenizer.decode(output_ids, skip_special_tokens=True).strip()
        return reasoning or None, "", False, raw_response

    answer_start = marker_start + len(marker_ids)
    reasoning = tokenizer.decode(
        output_ids[:marker_start], skip_special_tokens=True
    ).strip()
    reasoning = reasoning.removeprefix("<think>").lstrip()
    response = tokenizer.decode(
        output_ids[answer_start:], skip_special_tokens=True
    ).strip()
    return reasoning or None, response, True, raw_response


def LLM(
    prompt: str | None = None,
    *,
    history: Sequence[dict[str, str]] = (),
    model_name: str = DEFAULT_MODEL,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_new_tokens: int = 8_192,
    temperature: float = 0.6,
    top_p: float = 0.95,
    top_k: int = 20,
    min_p: float = 0.0,
    seed: int = 0,
    cache_dir: Path | None = None,
    allow_cpu: bool = False,
    reasoning_end_marker: str = DEFAULT_REASONING_END_MARKER,
) -> LLMOutput:
    """Generate once and retain the final response plus model-emitted reasoning.

    If ``prompt`` is omitted, this function asks for it interactively. Loaded
    model weights are cached and reused by later calls in the same process.
    """

    if prompt is None:
        prompt = input("You: ")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must not be empty")
    if not reasoning_end_marker:
        raise ValueError("reasoning_end_marker must not be empty")
    validate_generation_args(
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
    )

    cache_root = configure_cache(cache_dir)
    runtime = _load_runtime(model_name, str(cache_root), allow_cpu)
    torch = runtime.torch
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": prompt},
    ]
    model_inputs = runtime.tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
    model_inputs = {
        key: value.to(runtime.input_device) if hasattr(value, "to") else value
        for key, value in model_inputs.items()
    }
    prompt_tokens = int(model_inputs["input_ids"].shape[-1])

    generation_kwargs: dict[str, Any] = {
        "max_new_tokens": max_new_tokens,
        "do_sample": True,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "use_cache": True,
    }
    if (
        runtime.tokenizer.pad_token_id is None
        and runtime.tokenizer.eos_token_id is not None
    ):
        generation_kwargs["pad_token_id"] = runtime.tokenizer.eos_token_id

    try:
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            generated_ids = runtime.model.generate(
                **model_inputs, **generation_kwargs
            )
        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError as exc:
        raise RuntimeError(
            "CUDA ran out of memory. Use fewer generated tokens or a larger GPU."
        ) from exc

    elapsed = time.perf_counter() - started
    output_ids = generated_ids[0, prompt_tokens:].tolist()
    reasoning, response, complete, raw_response = _split_reasoning_output(
        output_ids, runtime.tokenizer, reasoning_end_marker
    )
    return LLMOutput(
        prompt=prompt,
        response=response,
        reasoning=reasoning,
        raw_response=raw_response,
        reasoning_complete=complete,
        generated_tokens=len(output_ids),
        generation_seconds=elapsed,
    )


def print_result(
    output: LLMOutput,
    show_reasoning: bool = False,
    *,
    show_stats: bool = False,
    file: TextIO | None = None,
) -> None:
    """Print an existing result without generating the prompt again."""

    destination = file or sys.stdout
    if show_reasoning:
        print("\n=== Reasoning trace (model-emitted) ===", file=destination)
        print(output.reasoning or "[No reasoning text was generated.]", file=destination)

    print("\n=== Response ===", file=destination)
    if output.reasoning_complete:
        print(
            output.response or "[The model emitted no final response.]",
            file=destination,
        )
    else:
        print(
            "[No final response could be separated. The reasoning was probably "
            "truncated; retry with a larger max_new_tokens value.]",
            file=destination,
        )

    if show_stats:
        print("\n=== Generation stats ===", file=destination)
        print(f"Generated tokens: {output.generated_tokens}", file=destination)
        print(f"Generation time:  {output.generation_seconds:.2f} s", file=destination)
        if output.generation_seconds > 0:
            throughput = output.generated_tokens / output.generation_seconds
            print(f"Throughput:       {throughput:.2f} tokens/s", file=destination)


def _read_multiline_prompt(transcript: TextIO | None) -> str | None:
    """Collect one chat turn until the user enters ``/send`` on its own line."""

    command_names = {
        "/clear",
        "/exit",
        "/quit",
        "/reasoning off",
        "/reasoning on",
        "/show",
    }
    lines: list[str] = []
    print("\nCompose your message. Enter /send on a new line when it is ready.")

    while True:
        try:
            line = input("│ ")
        except (EOFError, KeyboardInterrupt):
            if lines:
                print("\nDraft discarded.")
                if transcript is not None:
                    _write_system_message(
                        transcript, "Unsubmitted multiline draft discarded."
                    )
            return None

        command = line.strip().lower()
        has_message_text = any(existing.strip() for existing in lines)
        if not has_message_text and command in command_names:
            return command
        if command == "/cancel":
            print("Draft discarded.")
            if transcript is not None:
                _write_system_message(transcript, "Multiline draft discarded.")
            return ""
        if command == "/send":
            message = "\n".join(lines).strip()
            if message:
                return message
            print("The message is empty. Type some text, then enter /send.")
            if transcript is not None:
                _write_system_message(
                    transcript, "Empty message was not submitted."
                )
            lines.clear()
            continue
        lines.append(line)


def chat(
    *,
    model_name: str = DEFAULT_MODEL,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_new_tokens: int = 8_192,
    temperature: float = 0.6,
    top_p: float = 0.95,
    top_k: int = 20,
    min_p: float = 0.0,
    seed: int = 0,
    cache_dir: Path | None = None,
    allow_cpu: bool = False,
    show_reasoning: bool = False,
    show_stats: bool = False,
    transcript: TextIO | None = None,
) -> None:
    """Run a multi-turn terminal chat while keeping the model in memory."""

    history: list[dict[str, str]] = []
    last_output: LLMOutput | None = None
    print(
        "Type a multiline message, then enter /send on its own line. "
        "Commands before message text: /reasoning on, /reasoning off, "
        "/show, /clear, /quit. Use /cancel to discard a draft."
    )
    if transcript is not None:
        _write_system_message(
            transcript,
            "Interactive multiline chat started. Enter /send on its own line "
            "to submit. Commands before message text: /reasoning on, "
            "/reasoning off, /show, /clear, /quit. Use /cancel to discard a draft.",
        )

    while True:
        raw_prompt = _read_multiline_prompt(transcript)
        if raw_prompt is None:
            print("\nGoodbye.")
            if transcript is not None:
                _write_system_message(transcript, "Chat ended by the user.")
            return
        prompt = raw_prompt.strip()
        command = prompt.lower()
        if not prompt:
            continue
        if transcript is not None:
            _write_user_message(transcript, raw_prompt)
        if command in {"/quit", "/exit"}:
            print("Goodbye.")
            if transcript is not None:
                _write_system_message(transcript, "Chat ended by the user.")
            return
        if command == "/clear":
            history.clear()
            last_output = None
            print("Conversation cleared.")
            if transcript is not None:
                _write_system_message(transcript, "Conversation history cleared.")
            continue
        if command in {"/reasoning on", "/reasoning off"}:
            show_reasoning = command.endswith("on")
            state = "shown" if show_reasoning else "hidden"
            print(f"Reasoning will be {state}. Use /show to reprint the last result.")
            if transcript is not None:
                _write_system_message(
                    transcript,
                    f"Console reasoning display changed: {state}.",
                )
            continue
        if command == "/show":
            if last_output is None:
                print("There is no previous result to show.")
                if transcript is not None:
                    _write_system_message(transcript, "There is no result to reprint.")
            else:
                print_result(
                    last_output,
                    show_reasoning=show_reasoning,
                    show_stats=show_stats,
                )
                if transcript is not None:
                    _write_system_message(
                        transcript,
                        "The previous result was reprinted in the console; "
                        "its original transcript entry is above.",
                    )
            continue

        last_output = LLM(
            prompt,
            history=history,
            model_name=model_name,
            system_prompt=system_prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            seed=seed,
            cache_dir=cache_dir,
            allow_cpu=allow_cpu,
        )
        print_result(
            last_output,
            show_reasoning=show_reasoning,
            show_stats=show_stats,
        )
        if transcript is not None:
            _write_assistant_message(
                transcript,
                last_output,
                reasoning_open=show_reasoning,
            )
        if last_output.reasoning_complete:
            history.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": last_output.response},
                ]
            )


def _run(args: argparse.Namespace, transcript: TextIO) -> int:
    try:
        validate_generation_args(
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            min_p=args.min_p,
        )
        common_options = {
            "model_name": args.model,
            "system_prompt": args.system_prompt,
            "max_new_tokens": args.max_new_tokens,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "min_p": args.min_p,
            "seed": args.seed,
            "cache_dir": args.cache_dir,
            "allow_cpu": args.allow_cpu,
        }
        if args.prompt is None:
            _write_system_message(transcript, f"System prompt: {args.system_prompt}")
            chat(
                **common_options,
                show_reasoning=args.show_reasoning,
                show_stats=args.show_stats,
                transcript=transcript,
            )
        else:
            print(f"\nYou: {args.prompt}")
            _write_system_message(transcript, f"System prompt: {args.system_prompt}")
            _write_user_message(transcript, args.prompt)
            output = LLM(args.prompt, **common_options)
            print_result(
                output,
                show_reasoning=args.show_reasoning,
                show_stats=args.show_stats,
            )
            _write_assistant_message(
                transcript,
                output,
                reasoning_open=args.show_reasoning,
            )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _write_system_message(transcript, f"Error: {exc}")
        return 1
    return 0


def main() -> int:
    args = parse_args()
    output_path = _transcript_path(args.output_file)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects an existing transcript from being
        # overwritten when --output-file names a file that already exists.
        transcript = output_path.open("x", encoding="utf-8")
    except FileExistsError:
        print(
            f"Error: transcript already exists and was not overwritten: {output_path}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Error: could not create transcript {output_path}: {exc}", file=sys.stderr)
        return 1

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    console_capture = StringIO()
    with transcript:
        _write_transcript_header(transcript, output_path, args)
        with redirect_stdout(_Tee(original_stdout, console_capture)), redirect_stderr(
            _Tee(original_stderr, console_capture)
        ):
            print(f"Transcript: {output_path}")
            result = _run(args, transcript)
            finished = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"\nTranscript saved to: {output_path}")
            print(f"Finished: {finished}")
        _write_system_message(transcript, f"Run finished at {finished}.")
        _write_console_log(transcript, console_capture.getvalue())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
