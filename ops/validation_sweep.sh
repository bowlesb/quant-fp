#!/usr/bin/env bash
# Driver for the NIGHTLY parity-validation sweep (quantlib.features.validation_sweep).
#
# Proves live (source=stream) == backfill (source=backfill) for every symbol collected on the LAST MARKET
# DAY, contamination-aware, CHUNKED to stay memory-safe, and writes the trust LIFECYCLE + parity-defect
# backlog (docs/PARITY_LIFECYCLE.md). Runs in the baked fp-dev image against the PRODUCTION store volume
# (fp_store_real -> /store) and the DB, with Alpaca creds + DB_PASSWORD from .env. NEVER docker-exec's the
# live feature-computer.
#
# Idempotent/resumable: re-running a day refreshes the markers + lifecycle in place (upserts). Run AFTER
# market close AND after that day's raw backfill (ops/raw_backfill.sh) has landed in /store/raw.
#
#   ops/validation_sweep.sh                       # last market day, full discovered universe, chunk 200
#   DAY=2026-06-12 ops/validation_sweep.sh        # a specific settled day
#   MAX_SYMBOLS=50 ops/validation_sweep.sh        # evidence run: cap the discovered set (sandbox)
#   CHUNK=100 ops/validation_sweep.sh             # smaller chunks (tighter memory)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"

FEATURE_ROOT="${FEATURE_ROOT:-/store}"
VAL_ROOT="${VAL_ROOT:-/store/_validation}"
RAW_ROOT="${RAW_ROOT:-/store}"
CHUNK="${CHUNK:-200}"

ARGS=()
[ -n "${DAY:-}" ] && ARGS+=("$DAY")
ARGS+=("$FEATURE_ROOT" "$VAL_ROOT" "$RAW_ROOT" "--chunk" "$CHUNK")
[ -n "${MAX_SYMBOLS:-}" ] && ARGS+=("--max-symbols" "$MAX_SYMBOLS")
[ -n "${ALLOW_TODAY:-}" ] && ARGS+=("--allow-today")

docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.features.validation_sweep "${ARGS[@]}"
