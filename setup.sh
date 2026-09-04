#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: '$PYTHON_BIN' was not found." >&2
  echo "Set PYTHON_BIN to the Python executable in your RunPod PyTorch image." >&2
  exit 1
fi

if [[ ! -f requirements.txt ]]; then
  echo "Error: requirements.txt was not found in $ROOT_DIR" >&2
  exit 1
fi

echo "==> Checking the Python and preinstalled PyTorch environment"
"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10+ is required; found {sys.version.split()[0]}. "
        "Choose a newer RunPod PyTorch template."
    )

try:
    import torch
except ImportError as exc:
    raise SystemExit(
        "PyTorch is not installed. This setup intentionally does not install it. "
        "Start from a RunPod PyTorch pod, then rerun ./setup.sh."
    ) from exc

print(f"Python:  {sys.version.split()[0]}")
print(f"PyTorch: {torch.__version__}")

if torch.cuda.is_available():
    device_index = torch.cuda.current_device()
    props = torch.cuda.get_device_properties(device_index)
    vram_gib = props.total_memory / (1024**3)
    print(f"CUDA:    available ({torch.version.cuda})")
    print(f"GPU:     {props.name} ({vram_gib:.1f} GiB VRAM)")
else:
    print("CUDA:    not available")
    print(
        "Warning: setup can finish, but the default Qwen3.5 4B smoke test expects a GPU. "
        "Use --allow-cpu only for a very slow CPU test."
    )
PY

echo
echo "==> Installing lightweight Hugging Face dependencies"
echo "    PyTorch is deliberately absent from requirements.txt."
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install \
  --upgrade \
  --upgrade-strategy only-if-needed \
  -r requirements.txt

echo
echo "==> Verifying imports"
"$PYTHON_BIN" - <<'PY'
import accelerate
import torch
import tqdm
import transformers

print(f"transformers: {transformers.__version__}")
print(f"accelerate:   {accelerate.__version__}")
print(f"tqdm:         {tqdm.__version__}")
print(f"torch:        {torch.__version__}")
PY

echo
echo "Setup complete."
echo "Next command:"
echo "  $PYTHON_BIN hello_qwen.py"
