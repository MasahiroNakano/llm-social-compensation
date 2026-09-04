#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -f pyproject.toml ]; then
    uv init
fi

uv add transformers accelerate safetensors
uv add --dev ruff
uv add torch --index https://download.pytorch.org/whl/cu128

cat >> pyproject.toml <<'TOML'

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I"]
TOML

uv lock
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

uv run ruff format .
uv run ruff check .

echo "=== Bootstrap complete ==="
echo "Commit pyproject.toml and uv.lock to git."