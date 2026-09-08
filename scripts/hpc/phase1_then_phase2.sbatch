#!/usr/bin/env bash
#SBATCH --job-name=phase_1_2
#SBATCH --partition=gpu_lowp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --gres=gpu:1
#SBATCH --time=12:00:00
#SBATCH --requeue
#SBATCH --output=/nfs/nhome/live/mnakano/slurm-logs/llm-social-compensation/slurm-%x-%j.out
#SBATCH --error=/nfs/nhome/live/mnakano/slurm-logs/llm-social-compensation/slurm-%x-%j.err

nvidia-smi

set -euo pipefail

REPO_DIR=/nfs/nhome/live/mnakano/src/llm-social-compensation
cd "$REPO_DIR"

export MY_SCRATCH=/ceph/scratch/mnakano
export HF_HOME=/ceph/scratch/mnakano/huggingface
export PYTHONUNBUFFERED=1

source /ceph/scratch/mnakano/venvs/transformers/bin/activate

PHASE1_OUTPUT="$REPO_DIR/outputs/qwen35_phase1_single_turn.jsonl"
phase1_args=()
if [[ -f "$PHASE1_OUTPUT" ]]; then
    phase1_args+=(--resume)
fi

python scripts/phase1_single_turn.py "${phase1_args[@]}"
python scripts/phase2_two_turns.py --resume

