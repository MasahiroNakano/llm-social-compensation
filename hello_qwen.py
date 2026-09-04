#!/usr/bin/env python3
"""Run a minimal Hugging Face generation smoke test with Qwen3.5 4B.

PyTorch is expected to come from the RunPod PyTorch image. This script does not
install or modify PyTorch.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "Qwen/Qwen3.5-4B"
DEFAULT_PROMPT = (
    "In two concise sentences, explain why sanity checks matter in LLM safety "
    "experiments."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and run a small generation test with Qwen3.5 4B."
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face model ID (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="User prompt sent to the model.",
    )
    parser.add_argument(
        "--system-prompt",
        default="You are a concise, helpful research assistant.",
        help="System prompt used in the chat template.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=128,
        help="Maximum number of tokens to generate (default: 128).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature. Use 0 for deterministic greedy decoding.",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.9,
        help="Nucleus-sampling cutoff when temperature is above 0.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed (default: 0).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Hugging Face cache root. By default, use HF_HOME if set; otherwise "
            "use /workspace/.cache/huggingface when /workspace is writable."
        ),
    )
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Allow CPU inference when CUDA is unavailable. This will be very slow.",
    )
    return parser.parse_args()


def configure_environment(cache_dir: Path | None) -> Path:
    """Set Hugging Face cache variables before importing Hugging Face libraries."""
    if cache_dir is not None:
        chosen = cache_dir.expanduser().resolve()
        os.environ["HF_HOME"] = str(chosen)
    elif "HF_HOME" in os.environ:
        chosen = Path(os.environ["HF_HOME"]).expanduser()
    else:
        runpod_volume = os.environ.get("RUNPOD_VOLUME_PATH")
        candidates = [Path(runpod_volume)] if runpod_volume else []
        candidates.append(Path("/workspace"))

        writable_root = next(
            (
                path
                for path in candidates
                if path.is_dir() and os.access(path, os.W_OK)
            ),
            Path.home(),
        )
        chosen = writable_root / ".cache" / "huggingface"
        os.environ["HF_HOME"] = str(chosen)

    chosen.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_CACHE", str(chosen / "hub"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return chosen


def positive_int(value: int, name: str) -> None:
    if value <= 0:
        raise SystemExit(f"--{name} must be greater than 0; got {value}.")


def validate_sampling_args(temperature: float, top_p: float) -> None:
    if temperature < 0:
        raise SystemExit(f"--temperature must be non-negative; got {temperature}.")
    if not 0 < top_p <= 1:
        raise SystemExit(f"--top-p must be in (0, 1]; got {top_p}.")


def choose_dtype(torch_module: Any, using_cuda: bool) -> Any:
    if not using_cuda:
        return torch_module.float32
    if torch_module.cuda.is_bf16_supported():
        return torch_module.bfloat16
    return torch_module.float16


def dtype_name(dtype: Any) -> str:
    return str(dtype).removeprefix("torch.")


def input_device_for(model: Any) -> Any:
    """Find the device that should receive input IDs for a possibly sharded model."""
    embeddings = model.get_input_embeddings()
    if embeddings is not None and hasattr(embeddings, "weight"):
        return embeddings.weight.device
    return next(model.parameters()).device


def load_model(
    *,
    model_id: str,
    dtype: Any,
    using_cuda: bool,
    transformers_version: str,
    auto_model_class: Any,
) -> Any:
    major_version_text = transformers_version.split(".", maxsplit=1)[0]
    try:
        major_version = int(major_version_text)
    except ValueError:
        major_version = 4

    kwargs: dict[str, Any] = {"low_cpu_mem_usage": True}
    if using_cuda:
        kwargs["device_map"] = "auto"

    # Transformers 5 renamed torch_dtype to dtype. Keep compatibility with both
    # major versions so the script remains easy to reuse.
    if major_version >= 5:
        kwargs["dtype"] = dtype
    else:
        kwargs["torch_dtype"] = dtype

    model = auto_model_class.from_pretrained(model_id, **kwargs)
    if not using_cuda:
        model.to("cpu")
    model.eval()
    return model


def main() -> int:
    args = parse_args()
    positive_int(args.max_new_tokens, "max-new-tokens")
    validate_sampling_args(args.temperature, args.top_p)
    cache_root = configure_environment(args.cache_dir)

    try:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        print(
            "Missing dependency. Run ./setup.sh in a RunPod PyTorch pod first.",
            file=sys.stderr,
        )
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

    using_cuda = torch.cuda.is_available()
    if not using_cuda and not args.allow_cpu:
        print(
            "CUDA is not available. The default 4B model is intended for a GPU pod.\n"
            "Check `nvidia-smi` and your RunPod template, or rerun with --allow-cpu "
            "for a very slow CPU test.",
            file=sys.stderr,
        )
        return 2

    torch.manual_seed(args.seed)
    if using_cuda:
        torch.cuda.manual_seed_all(args.seed)

    dtype = choose_dtype(torch, using_cuda)

    print("=== Environment ===")
    print(f"Python:       {sys.version.split()[0]}")
    print(f"PyTorch:      {torch.__version__}")
    print(f"Transformers: {transformers.__version__}")
    print(f"HF cache:     {cache_root}")
    print(f"Model:        {args.model}")
    print(f"Precision:    {dtype_name(dtype)}")

    if using_cuda:
        index = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(index)
        print(f"CUDA:         {torch.version.cuda}")
        print(f"GPU:          {props.name}")
        print(f"VRAM:         {props.total_memory / (1024**3):.1f} GiB")
    else:
        print("Device:       CPU")

    print("\nLoading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(args.model)

    print("Loading model (the first run downloads its weights)...")
    try:
        model = load_model(
            model_id=args.model,
            dtype=dtype,
            using_cuda=using_cuda,
            transformers_version=transformers.__version__,
            auto_model_class=AutoModelForCausalLM,
        )
    except torch.cuda.OutOfMemoryError:
        print(
            "CUDA ran out of memory while loading the model. Stop other GPU "
            "processes or choose a pod with more VRAM.",
            file=sys.stderr,
        )
        return 3

    messages = [
        {"role": "system", "content": args.system_prompt},
        {"role": "user", "content": args.prompt},
    ]
    model_inputs = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        enable_thinking=False,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )

    input_device = input_device_for(model)
    model_inputs = {
        key: value.to(input_device) if hasattr(value, "to") else value
        for key, value in model_inputs.items()
    }
    prompt_tokens = int(model_inputs["input_ids"].shape[-1])

    generation_kwargs: dict[str, Any] = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": args.temperature > 0,
        "use_cache": True,
    }
    if args.temperature > 0:
        generation_kwargs.update(
            temperature=args.temperature,
            top_p=args.top_p,
        )
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        generation_kwargs["pad_token_id"] = tokenizer.eos_token_id

    print("Generating...\n")
    try:
        if using_cuda:
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.inference_mode():
            generated_ids = model.generate(**model_inputs, **generation_kwargs)
        if using_cuda:
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
    except torch.cuda.OutOfMemoryError:
        print(
            "CUDA ran out of memory during generation. Lower --max-new-tokens "
            "or use a pod with more VRAM.",
            file=sys.stderr,
        )
        return 4

    new_token_ids = generated_ids[:, prompt_tokens:]
    generated_tokens = int(new_token_ids.shape[-1])
    response = tokenizer.batch_decode(
        new_token_ids,
        skip_special_tokens=True,
    )[0].strip()

    print("=== Model response ===")
    print(response)
    print("\n=== Smoke test passed ===")
    print(f"Generated tokens: {generated_tokens}")
    print(f"Generation time:  {elapsed:.2f} s")
    if elapsed > 0:
        print(f"Throughput:       {generated_tokens / elapsed:.2f} tokens/s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
