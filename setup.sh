#!/usr/bin/env bash
set -euo pipefail

REPO_URL="git@github.com:MasahiroNakano/llm-social-compensation.git"
REPO_DIR="/workspace/llm-social-compensation"

export HF_HOME="/workspace/hf_cache"
mkdir -p "$HF_HOME"

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
    git clone "$REPO_URL" "$REPO_DIR"
else
    cd "$REPO_DIR"

    if [ -n "$(git status --porcelain)" ]; then
        echo "ERROR: uncommitted changes exist"
        git status --short
        exit 1
    fi

    git pull --ff-only
fi

cd "$REPO_DIR"

uv sync

echo "=== CUDA check ==="

uv run python - <<'PY'
import torch

print("torch:", torch.__version__)
print("torch CUDA:", torch.version.cuda)
print("CUDA available:", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available")

print("GPU:", torch.cuda.get_device_name(0))
PY

echo "=== Ruff check ==="

uv run ruff check .

echo "=== Ready ==="
echo "Repo: $REPO_DIR"
echo "HF_HOME: $HF_HOME"