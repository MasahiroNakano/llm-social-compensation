# LLM Safety & Interpretability Experiments

A deliberately small, RunPod-first starter repository for LLM safety and
interpretability experiments.

The first milestone is only an environment smoke test: install the lightweight
Hugging Face dependencies without replacing the PyTorch/CUDA build supplied by
the RunPod PyTorch image, then load an official Qwen 3 4B model and generate one
response.

The provided social-compensation research plan is preserved in
[`PROJECT_PLAN.md`](PROJECT_PLAN.md). Experiment runners, datasets, judges, and
analysis code will be added after this basic GPU/model setup is confirmed.

## Repository contents

```text
.
├── .gitignore
├── PROJECT_PLAN.md
├── README.md
├── chat_qwen.py
├── hello_qwen.py
├── hello_qwen_reasoning.py
├── requirements.txt
└── setup.sh
```

- `setup.sh` checks Python, the preinstalled PyTorch build, CUDA visibility, and
  then installs only `transformers` and `accelerate`.
- `hello_qwen.py` downloads and runs
  [`Qwen/Qwen3-4B-Instruct-2507`](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
  by default.
- `hello_qwen_reasoning.py` runs one prompt with the Qwen thinking checkpoint
  and separates the model-emitted reasoning from its final answer.
- `chat_qwen.py` provides a reusable Python interface and an interactive,
  multi-turn chat with optional reasoning display.
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
python3 hello_qwen.py
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
python3 hello_qwen.py --help
```

Use a custom prompt:

```bash
python3 hello_qwen.py \
  --prompt "Give one example of a confound in a sequential LLM evaluation."
```

Enable sampling:

```bash
python3 hello_qwen.py \
  --temperature 0.7 \
  --top-p 0.9 \
  --seed 42
```

Use another compatible Hugging Face model:

```bash
python3 hello_qwen.py --model Qwen/Qwen3-4B
```

The default is the non-thinking 4B instruct checkpoint because this first test
only needs to verify ordinary chat generation. The model is configurable so a
reasoning-capable checkpoint can be used in later experiments.
The official model card reports 4.0B parameters and requires
`transformers>=4.51.0`; the repository pins the compatible Transformers 4.x
major series for a conservative first setup.

## Interactive chat and reusable results

Start a normal multi-turn chat with the reasoning-capable checkpoint:

```bash
python3 chat_qwen.py
```

Within the chat, use `/reasoning on` or `/reasoning off` to choose whether the
trace is displayed. `/show` reprints the last result with the current display
setting without generating it again. `/clear` clears the conversation and
`/quit` exits.

In a Python session or notebook, generation and printing are separate:

```python
from chat_qwen import LLM, print_result

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
python3 chat_qwen.py --prompt "What is activation steering?" --show-reasoning
```

## Model cache on RunPod

`hello_qwen.py` chooses the cache in this order:

1. `--cache-dir`, when provided;
2. an existing `HF_HOME` environment variable;
3. `$RUNPOD_VOLUME_PATH/.cache/huggingface`, when that path exists and is
   writable;
4. `/workspace/.cache/huggingface`, when `/workspace` is writable;
5. `~/.cache/huggingface` as a fallback.

You can always choose the path explicitly:

```bash
python3 hello_qwen.py \
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
python3 hello_qwen.py
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
python3 hello_qwen.py
```

## What is deliberately not included yet

There is no `pyproject.toml`, package installation, notebook stack, experiment
framework, quantization dependency, FlashAttention build, vLLM server, or
mechanistic-interpretability library. Those should be added only when the next
experiment actually needs them.
