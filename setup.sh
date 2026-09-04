#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/workspace/llm-social-compensation"
VENV_DIR="/root/.venvs/llm-social-compensation"
HF_CACHE="/workspace/hf_cache"
UV_CACHE="/root/.cache/uv"

cd "$REPO_DIR"

echo "=== Checking RunPod PyTorch ==="

python - <<'PY'
import torch

print("System torch:", torch.__version__)
print("Torch CUDA:", torch.version.cuda)
print("CUDA available:", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise RuntimeError(
        "RunPod system PyTorch cannot access CUDA. "
        "Check the pod template / CUDA compatibility."
    )

print("GPU:", torch.cuda.get_device_name(0))
PY

echo
echo "=== Installing uv if needed ==="

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo
echo "=== Updating repository ==="

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: repository has uncommitted changes:"
    git status --short
    exit 1
fi

git pull --ff-only

echo
echo "=== Configuring storage ==="

mkdir -p "$HF_CACHE"
mkdir -p "$(dirname "$VENV_DIR")"
mkdir -p "$UV_CACHE"

export HF_HOME="$HF_CACHE"
export UV_CACHE_DIR="$UV_CACHE"
export UV_PROJECT_ENVIRONMENT="$VENV_DIR"

# Remove old project-local venv if one exists on the network volume.
if [ -e "$REPO_DIR/.venv" ]; then
    echo "Removing old network-volume .venv..."
    rm -rf "$REPO_DIR/.venv"
fi

echo
echo "=== Creating pod-local virtualenv ==="

if [ ! -d "$VENV_DIR" ]; then
    uv venv \
        --python "$(command -v python)" \
        --system-site-packages \
        "$VENV_DIR"
fi

echo
echo "=== Installing locked project dependencies ==="

uv sync --frozen

echo
echo "=== Verifying environment ==="

uv run python - <<'PY'
import torch
import transformers

print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("torch CUDA:", torch.version.cuda)
print("CUDA available:", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise RuntimeError("CUDA isn't visible from the uv environment.")

print("GPU:", torch.cuda.get_device_name(0))
PY

echo
echo "=== Ruff ==="

uv run ruff check .

echo
echo "=== Persisting shell environment ==="

HF_LINE='export HF_HOME="/workspace/hf_cache"'
UV_ENV_LINE='export UV_PROJECT_ENVIRONMENT="/root/.venvs/llm-social-compensation"'
UV_CACHE_LINE='export UV_CACHE_DIR="/root/.cache/uv"'

grep -qxF "$HF_LINE" ~/.bashrc || echo "$HF_LINE" >> ~/.bashrc
grep -qxF "$UV_ENV_LINE" ~/.bashrc || echo "$UV_ENV_LINE" >> ~/.bashrc
grep -qxF "$UV_CACHE_LINE" ~/.bashrc || echo "$UV_CACHE_LINE" >> ~/.bashrc

echo
echo "=== Pod ready ==="
echo "Repo:       $REPO_DIR"
echo "Venv:       $VENV_DIR"
echo "HF cache:   $HF_CACHE"
echo
echo "Open a new terminal, or run:"
echo "  source ~/.bashrc"
echo
echo "Then:"
echo "  cd $REPO_DIR"
echo "  uv run python hello_qwen.py"