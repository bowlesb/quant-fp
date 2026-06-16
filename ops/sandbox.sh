#!/usr/bin/env bash
# Run a command in a RESOURCE-CAPPED, ISOLATED container — the standard sandbox for ALL subagent / explorer
# heavy compute, so a runaway job can never OOM the live capture or starve the 32-core box.
#
# - Hard memory cap (--memory == --memory-swap, so NO swap → the JOB is OOM-killed at the limit, never the host).
# - CPU cap.
# - Mounts the repo at /app (code + experiments/data scratch) and the prod store READ-ONLY at /store
#   (so a sandbox can READ /store/raw + the feature store, but can NEVER write production data).
# - Auto-removed (--rm). Alpaca/DB creds via .env; on the quant_default network for DB + Alpaca.
#
# Usage:   ops/sandbox.sh "python my_analysis.py --foo bar"
#          MEM=16g CPUS=8 ops/sandbox.sh "python -m quantlib.research.fit ..."
# Defaults: MEM=8g CPUS=4 (raise only with reason; the MA grants the budget per explorer).
set -euo pipefail

REPO=/home/ben/quant-fp
MEM="${MEM:-8g}"
CPUS="${CPUS:-4}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOL="${STORE_VOL:-fp_store_real}"
ENV_FILE="$REPO/.env"

if [ "$#" -eq 0 ]; then
  echo "usage: [MEM=8g] [CPUS=4] $0 \"<command>\"" >&2
  exit 2
fi

ENV_ARG=()
[ -f "$ENV_FILE" ] && ENV_ARG=(--env-file "$ENV_FILE")

exec docker run --rm \
  --memory="$MEM" --memory-swap="$MEM" --cpus="$CPUS" \
  --network quant_default \
  "${ENV_ARG[@]}" \
  -e DB_PASSWORD="${DB_PASSWORD:-mock}" \
  -e POLARS_MAX_THREADS="${CPUS}" \
  -v "$REPO":/app \
  -v "$STORE_VOL":/store:ro \
  -w /app \
  "$IMAGE" sh -c "$*"
