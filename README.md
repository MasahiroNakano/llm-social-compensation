# Manual notes

note that, `setup.sh` currently has been tested for Runpod environment, 
- CUDA version 12.8, 
- RTX 4090, 
- Runpod Pytorch 2.4.0
- runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

For SLURM environment, see ~/using transformer in slurm.sh
----

# LLM Safety & Interpretability Experiments

A resumable two-phase Qwen3.5 4B experiment for measuring how a model's previous
recommendation affects its evaluation of a second proposal. Both phases use
temperature 1.0 and are designed to run sequentially on one GPU.

The original social-compensation research plan is preserved in
[`PROJECT_PLAN.md`](PROJECT_PLAN.md); the current runnable experiment is defined
by the two phase scripts and `prompts/phase1_four_prompts.json`.

## Repository contents

```text
.
├── .gitignore
├── PROJECT_PLAN.md
├── README.md
├── jsonl_to_markdown.py
├── prompts/
│   └── phase1_four_prompts.json
├── requirements.txt
├── scripts/
│   ├── matched_verdict_phase2_qwen.py
│   ├── matched_verdict_phase2_qwen_exp2.py
│   ├── phase1_single_turn.py
│   ├── phase2_two_turns.py
│   └── smoke_test/
│       ├── batch_inference.py
│       ├── chat_qwen.py
│       ├── hello_qwen.py
│       └── hello_qwen_reasoning.py
├── src/
│   ├── batch_qwen.py
│   ├── qwen_runtime.py
│   └── two_turn_batch_qwen.py
└── setup.sh
```

- `setup.sh` checks Python, the preinstalled PyTorch build, CUDA visibility, and
  then installs only `transformers` and `accelerate`.
- `scripts/smoke_test/hello_qwen.py` downloads and runs
  [`Qwen/Qwen3.5-4B`](https://huggingface.co/Qwen/Qwen3.5-4B) in non-thinking
  mode by default.
- `scripts/smoke_test/hello_qwen_reasoning.py` runs one prompt with Qwen3.5's default
  thinking mode and separates the model-emitted reasoning from its final answer.
- `scripts/smoke_test/chat_qwen.py` provides a reusable Python interface and an
  interactive, multi-turn chat with optional reasoning display.
- `scripts/phase1_single_turn.py` samples the four proposals 16 times each.
- `scripts/phase2_two_turns.py` runs all 12 ordered pairs, sampling eight
  second-turn responses after each saved first-turn response.
- `src/batch_qwen.py` and `src/two_turn_batch_qwen.py` are the canonical engines
  behind the two phase runners.
- `jsonl_to_markdown.py` converts batch JSONL results into readable Markdown,
  with responses shown directly and reasoning traces in collapsible sections.
- `prompts/phase1_four_prompts.json` stores P1-P4 and the two prompt endings.
- `requirements.txt` intentionally contains no `torch` dependency.
- `PROJECT_PLAN.md` is the supplied research plan, unchanged.

## RunPod quick start

Start a modern **RunPod PyTorch GPU pod**, open its terminal, and put the
repository somewhere persistent, normally under `/workspace`.

```bash
cd /workspace
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY

chmod +x setup.sh
./setup.sh
python3 -m scripts.smoke_test.hello_qwen
```

A successful run ends with:

```text
=== Smoke test passed ===
```

## Publish the extracted folder to GitHub

After creating an empty repository on GitHub, run from this folder:

```bash
git init -b main
git add .
git commit -m "Initial RunPod Qwen smoke test"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

The first run takes longer because it downloads the model weights. Later runs
reuse the Hugging Face cache.

## Why this uses pip rather than uv

This initial repository intentionally has no package build and no isolated
virtual environment. A RunPod PyTorch image already owns the heavyweight,
CUDA-matched PyTorch installation. Installing two lightweight dependencies into
the current Python environment is the least surprising path and avoids an
isolated environment that cannot see the preinstalled PyTorch build.

`setup.sh` is safe to rerun. It uses the selected Python executable's pip and
keeps PyTorch out of `requirements.txt`.

To select a different Python executable:

```bash
PYTHON_BIN=python3.11 ./setup.sh
```

## Model and generation options

Show all options:

```bash
python3 -m scripts.smoke_test.hello_qwen --help
```

Use a custom prompt:

```bash
python3 -m scripts.smoke_test.hello_qwen \
  --prompt "Give one example of a confound in a sequential LLM evaluation."
```

Enable sampling:

```bash
python3 -m scripts.smoke_test.hello_qwen \
  --temperature 0.7 \
  --top-p 0.9 \
  --seed 42
```

Use another compatible Hugging Face model:

```bash
python3 -m scripts.smoke_test.hello_qwen --model Qwen/Qwen3.5-4B
```

The default is the unified Qwen3.5 4B checkpoint with thinking disabled in the
chat template because this first test only needs to verify ordinary chat
generation. The reasoning and experiment runners use the same checkpoint's
default thinking mode. The official model card reports a 4B language model and
requires a recent Transformers release; the repository pins the compatible
Transformers 5.13.1 release so it remains compatible with PyTorch 2.4 in the
RunPod image.

## Interactive chat and reusable results

Start a normal multi-turn chat with the reasoning-capable checkpoint:

```bash
python3 -m scripts.smoke_test.chat_qwen
```

Interactive input is multiline. Press Enter to add another line, then enter
`/send` on its own line to submit the entire message as one user turn. Before
typing message text, use `/reasoning on` or `/reasoning off` to choose whether
the trace is displayed. `/show` reprints the last result with the current
display setting without generating it again, `/clear` clears the conversation,
and `/quit` exits. `/cancel` discards a draft being composed.

Each invocation creates one timestamped Markdown transcript under `outputs/`.
User messages are right-aligned, assistant messages are left-aligned, and
system/status messages are centered. Every reasoning trace and generation stat
is retained; reasoning is expanded when console reasoning is on and collapsed
when it is off. A collapsed appendix also preserves the complete console log.
Generated transcripts are ignored by Git. To choose a `.md` or `.txt` path
yourself, use:

```bash
python3 -m scripts.smoke_test.chat_qwen --output-file outputs/my_chat.md
```

An existing file is never overwritten.

In a Python session or notebook, generation and printing are separate:

```python
from scripts.smoke_test.chat_qwen import LLM, print_result

output = LLM("Why do control experiments matter?")

print_result(output)                       # final response only
print_result(output, show_reasoning=True)  # reasoning and final response
```

Calling `output = LLM()` with no prompt asks for one interactively. The same
`output` can be passed to `print_result` any number of times without rerunning
the model. Its `response`, `reasoning`, and `raw_response` fields can also be
accessed directly.

For one terminal prompt, use:

```bash
python3 -m scripts.smoke_test.chat_qwen \
  --prompt "What is activation steering?" \
  --show-reasoning
```

## Run the experiment

Phase 1 generates 16 independent responses for each of P1-P4, for 64 total
generations. Its deterministic output is
`outputs/qwen35_phase1_single_turn.jsonl`.

```bash
python3 scripts/phase1_single_turn.py
```

After phase 1 completes, phase 2 runs all 12 directed pairs. It loads the model
once and creates one JSONL file per pair under
`outputs/phase2_two_turns_temperature_1/`.

```bash
python3 scripts/phase2_two_turns.py
```

If either process is interrupted, rerun that phase with `--resume`:

```bash
python3 scripts/phase1_single_turn.py --resume
python3 scripts/phase2_two_turns.py --resume
```

Resume mode validates existing records, appends only missing sample IDs, and
repairs an incomplete final JSONL line left by abrupt job termination. Use
`--batch-size 4` if the default batch size of 8 exceeds GPU memory. Both scripts
also support `--dry-run`; phase 2 additionally supports repeatable selectors
such as `--pair P1:P2`.

## Convert batch output to Markdown

Convert one JSONL file to a same-named Markdown file beside it:

```bash
python3 jsonl_to_markdown.py outputs/qwen35_phase1_single_turn.jsonl
```

Convert every JSONL file under `outputs/`, replacing any existing Markdown
renderings:

```bash
python3 jsonl_to_markdown.py outputs/*.jsonl --force
```

Use `--output PATH` to name the result for one input, or `--output-dir DIR` to
put results for one or more inputs in another directory. By default, redundant
`raw_response` values are omitted because the parsed reasoning and response are
already shown. Pass `--include-raw` to retain them as collapsible sections too.

## Model cache on RunPod

`scripts/smoke_test/hello_qwen.py` chooses the cache in this order:

1. `--cache-dir`, when provided;
2. an existing `HF_HOME` environment variable;
3. `$RUNPOD_VOLUME_PATH/.cache/huggingface`, when that path exists and is
   writable;
4. `/workspace/.cache/huggingface`, when `/workspace` is writable;
5. `~/.cache/huggingface` as a fallback.

You can always choose the path explicitly:

```bash
python3 -m scripts.smoke_test.hello_qwen \
  --cache-dir /workspace/.cache/huggingface
```

This matters because model weights are much larger than this Git repository and
should live on persistent storage when available.

## Hugging Face authentication

The default model is public, so an access token is normally unnecessary. If the
Hub asks for authentication or you encounter anonymous download limits, set a
token in the shell without committing it:

```bash
export HF_TOKEN="hf_..."
python3 -m scripts.smoke_test.hello_qwen
```

`.env` files are ignored by Git, but this starter does not load them
automatically.

## Expected hardware

The default checkpoint has 4 billion parameters. In half precision, the model
weights alone are roughly 8 GB, before runtime buffers and the KV cache. A GPU
with comfortable headroom is preferable for the unquantized smoke test.

The script selects BF16 on supported GPUs, FP16 on other CUDA GPUs, and refuses
to run on CPU unless `--allow-cpu` is explicitly supplied.

## Troubleshooting

### `PyTorch is not installed`

The repository intentionally does not install PyTorch. Recreate the pod from a
RunPod PyTorch template and rerun `./setup.sh`.

### `CUDA is not available`

Check:

```bash
nvidia-smi
python3 -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
```

If `nvidia-smi` works but PyTorch reports `False`, the current Python environment
likely does not contain the CUDA-enabled PyTorch build from the pod image.

### CUDA out of memory

Stop other GPU processes, choose a GPU with more VRAM, or test a smaller model.
Quantization is deliberately not added yet because this repository is meant to
validate the simplest standard Transformers path first.

### Cache is filling the wrong disk

Set the cache explicitly:

```bash
export HF_HOME=/workspace/.cache/huggingface
python3 -m scripts.smoke_test.hello_qwen
```

## What is deliberately not included yet

There is no `pyproject.toml`, package installation, notebook stack, experiment
framework, quantization dependency, FlashAttention build, vLLM server, or
mechanistic-interpretability library. Those should be added only when the next
experiment actually needs them.
