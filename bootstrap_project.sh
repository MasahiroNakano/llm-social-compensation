#!/usr/bin/env bash
set -euo pipefail

cd /workspace/llm-social-compensation

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

cat > pyproject.toml <<'TOML'
[project]
name = "llm-social-compensation"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "transformers>=5.16.1",
    "safetensors",
]

[dependency-groups]
dev = [
    "ruff",
]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I"]
TOML

echo "=== Creating lockfile ==="

rm -f uv.lock
uv lock

echo
echo "=== Bootstrap complete ==="
echo "Created:"
echo "  pyproject.toml"
echo "  uv.lock"
echo
echo "No PyTorch dependency is managed by uv."
echo "PyTorch comes from the RunPod template."