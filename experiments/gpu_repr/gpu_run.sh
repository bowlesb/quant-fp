#!/usr/bin/env bash
# GPU lock wrapper — follows the MA-owned protocol in experiments/GPU_QUEUE.md.
# Serializes all GPU jobs on the single 3090 via ~/.quant-gpu.lock. Usage:
#   experiments/gpu_repr/gpu_run.sh <jobname> -- <command...>
# Example:
#   experiments/gpu_repr/gpu_run.sh repr-vae-z16 -- \
#     /home/ben/quant-fp/experiments/dl_research/.venv/bin/python experiments/gpu_repr/train_vae.py --panel ...
set -euo pipefail

JOBNAME="${1:?usage: gpu_run.sh <jobname> -- <command...>}"
shift
[ "${1:-}" = "--" ] && shift

LOCK="$HOME/.quant-gpu.lock"
( set -o noclobber; echo "$$ $(date -u +%FT%TZ) ${JOBNAME}" > "$LOCK" ) 2>/dev/null \
  || { echo "GPU busy:"; cat "$LOCK"; exit 1; }
trap 'rm -f "$LOCK"' EXIT

echo "[gpu_run] acquired lock for ${JOBNAME} (pid $$)"
"$@"
echo "[gpu_run] ${JOBNAME} done; releasing lock"
