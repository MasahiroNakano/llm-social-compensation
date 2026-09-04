#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-setup}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${REPO_DIR}/.venv}"

usage() {
  cat <<'EOF'
Usage: ./setup_slurm.sh [setup|inventory|check]

Modes:
  setup      Create/update .venv and verify dependencies (default).
             If running in a Slurm allocation, also check the actual GPU.
  inventory  Show GPU resources/features advertised by Slurm. No allocation.
  check      Inspect CUDA and run a PyTorch GPU test inside an allocation.

Environment variables:
  PYTHON_BIN       Base Python executable (default: python3).
  VENV_DIR         Virtual environment path (default: <repo>/.venv).
  TORCH_INDEX_URL  Optional PyTorch wheel index. PyTorch is installed only when
                   it is missing and this variable is explicitly provided.

Examples:
  ./setup_slurm.sh inventory
  module load <your-cluster-python-or-pytorch-module>
  ./setup_slurm.sh setup
  srun <your-cluster-gpu-options> --pty bash
  source .venv/bin/activate
  ./setup_slurm.sh check
EOF
}

slurm_inventory() {
  if ! command -v sinfo >/dev/null 2>&1; then
    echo "Error: sinfo is unavailable. Run this on a Slurm login node." >&2
    return 1
  fi

  echo "==> Slurm GPU inventory (controller metadata; no allocation requested)"
  sinfo --Node --format="NodeHost:24,Partition:18,StateCompact:10,Gres:36,Features:60"
  echo
  echo "GRES and Features may identify L40S/A100/H100 nodes if the cluster"
  echo "administrator configured those labels. They do not report the live"
  echo "NVIDIA driver or CUDA runtime installed on a compute node."
}

choose_environment_python() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    printf '%s\n' "${VENV_DIR}/bin/python"
  else
    printf '%s\n' "${PYTHON_BIN}"
  fi
}

check_gpu() {
  local environment_python
  environment_python="$(choose_environment_python)"

  echo "==> Allocation context"
  echo "Host:               $(hostname)"
  echo "SLURM_JOB_ID:       ${SLURM_JOB_ID:-not set}"
  echo "SLURM_JOB_PARTITION:${SLURM_JOB_PARTITION:+ }${SLURM_JOB_PARTITION:-not set}"
  echo "CUDA_VISIBLE_DEVICES: ${CUDA_VISIBLE_DEVICES:-not set}"

  if [[ -z "${SLURM_JOB_ID:-}" ]]; then
    echo
    echo "Warning: SLURM_JOB_ID is not set. This does not look like an allocated node."
  fi

  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "Error: nvidia-smi is unavailable; no allocated NVIDIA GPU is visible." >&2
    return 1
  fi

  echo
  echo "==> NVIDIA driver and driver-supported CUDA level"
  nvidia-smi

  echo
  echo "==> CUDA toolkit compiler"
  if command -v nvcc >/dev/null 2>&1; then
    nvcc --version
  else
    echo "nvcc is not loaded. This is fine for prebuilt PyTorch wheels."
  fi

  if ! command -v "${environment_python}" >/dev/null 2>&1; then
    echo "Error: Python executable '${environment_python}' was not found." >&2
    return 1
  fi

  echo
  echo "==> PyTorch CUDA runtime and compute test"
  "${environment_python}" - <<'PY'
from __future__ import annotations

import sys

try:
    import torch
except ImportError as exc:
    raise SystemExit(
        "PyTorch is not installed in this environment. Load the cluster's "
        "PyTorch module or rerun setup with an explicit TORCH_INDEX_URL."
    ) from exc

print(f"Python:              {sys.version.split()[0]}")
print(f"PyTorch:             {torch.__version__}")
print(f"PyTorch CUDA runtime:{torch.version.cuda!s:>12}")
print(f"cuDNN:               {torch.backends.cudnn.version()}")
print(f"CUDA available:      {torch.cuda.is_available()}")

if not torch.cuda.is_available():
    raise SystemExit(
        "PyTorch cannot use CUDA. Its bundled runtime may be incompatible with "
        "the node driver, or this process may not have a GPU allocation."
    )

print(f"Visible GPU count:   {torch.cuda.device_count()}")
for index in range(torch.cuda.device_count()):
    properties = torch.cuda.get_device_properties(index)
    print(
        f"GPU {index}: {properties.name}; "
        f"compute capability {properties.major}.{properties.minor}; "
        f"{properties.total_memory / (1024**3):.1f} GiB"
    )

device = torch.device("cuda:0")
left = torch.randn((1024, 1024), device=device)
right = torch.randn((1024, 1024), device=device)
result = left @ right
torch.cuda.synchronize(device)
print(f"CUDA tensor test:    passed ({result.shape=}, {result.device=})")
PY
}

setup_environment() {
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Error: '${PYTHON_BIN}' was not found." >&2
    echo "Load your cluster's Python/PyTorch module or set PYTHON_BIN." >&2
    return 1
  fi
  if [[ ! -f "${REPO_DIR}/requirements.txt" ]]; then
    echo "Error: requirements.txt was not found in ${REPO_DIR}." >&2
    return 1
  fi

  "${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10+ is required; found {sys.version.split()[0]}.")
print(f"Base Python: {sys.executable} ({sys.version.split()[0]})")
PY

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "==> Creating ${VENV_DIR} with access to site-provided packages"
    "${PYTHON_BIN}" -m venv --system-site-packages "${VENV_DIR}"
  else
    echo "==> Reusing ${VENV_DIR}"
  fi

  local venv_python="${VENV_DIR}/bin/python"

  if ! "${venv_python}" -c "import torch" >/dev/null 2>&1; then
    if [[ -n "${TORCH_INDEX_URL:-}" ]]; then
      echo "==> PyTorch is missing; installing from the explicitly selected index"
      "${venv_python}" -m pip install torch --index-url "${TORCH_INDEX_URL}"
    else
      echo "Error: PyTorch is not visible inside ${VENV_DIR}." >&2
      echo "Load the cluster's PyTorch module before setup, or explicitly provide:" >&2
      echo "  TORCH_INDEX_URL=<compatible-pytorch-wheel-index> ./setup_slurm.sh setup" >&2
      echo "The script will not guess a CUDA/PyTorch build for your cluster." >&2
      return 1
    fi
  fi

  echo "==> Installing repository dependencies (PyTorch remains untouched)"
  "${venv_python}" -m pip install \
    --upgrade-strategy only-if-needed \
    -r "${REPO_DIR}/requirements.txt"

  echo "==> Verifying the environment"
  "${venv_python}" - <<'PY'
import accelerate
import torch
import transformers

print(f"PyTorch:     {torch.__version__}")
print(f"Torch CUDA:  {torch.version.cuda}")
print(f"Transformers:{transformers.__version__:>12}")
print(f"Accelerate:  {accelerate.__version__:>12}")
PY

  echo
  echo "Setup complete. In every Slurm job, load the same cluster modules, then run:"
  echo "  source '${VENV_DIR}/bin/activate'"
  echo "Set HF_HOME to a persistent project/scratch path for the model cache."

  if [[ -n "${SLURM_JOB_ID:-}" ]]; then
    echo
    check_gpu
  else
    echo "GPU verification was skipped because this is not a Slurm allocation."
    echo "After requesting a GPU, run: ./setup_slurm.sh check"
  fi
}

case "${MODE}" in
  setup)
    setup_environment
    ;;
  inventory)
    slurm_inventory
    ;;
  check)
    check_gpu
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Error: unknown mode '${MODE}'." >&2
    usage >&2
    exit 2
    ;;
esac

