#!/usr/bin/env bash
# Single entrypoint for the backfill spine: ACQUIRE -> MATERIALIZE -> VALIDATE.
#
# This is a thin DISPATCHER. It does NOT reimplement any logic — each subcommand delegates to the
# existing, working tool so behavior is unchanged and the reorg is reversible:
#
#   acquire      -> ops/raw_backfill.sh                      (quantlib.data.raw_backfill -> /store/raw)
#   materialize  -> python -m quantlib.features.materialize  (materialize_from_raw: /store/raw -> features)
#   validate     -> python -m quantlib.features.validate     (scoped stream-vs-backfill -> trust ledger)
#
# The three stages chain: acquire writes the raw tape ONCE, materialize computes features FROM that tape,
# validate certifies the live-collected features against that backfill. See docs/BACKFILL.md.
#
# Usage:
#   ops/backfill.sh acquire sample                          # AAPL,SPY,NVDA x 2 recent days -> /store/raw
#   ops/backfill.sh acquire full                            # 6mo top-1500/300 (the LEAD's long run)
#   ops/backfill.sh materialize <day> <n> [raw_root]        # materialize N liquid symbols for <day>
#   ops/backfill.sh validate <day> <feat_root> <val_root> [--allow-today] [--symbols AAPL,MSFT,...]
#
# The materialize/validate subcommands run inside the fp-dev image with the prod store volume and
# Alpaca/DB creds, mirroring ops/raw_backfill.sh. Heavy runs should go through ops/sandbox.sh's caps.
set -euo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  # Print the leading comment header (every line up to the first non-comment, blank line excluded).
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
  exit "${1:-2}"
}

run_module() {
  # Run a quantlib module inside fp-dev with the prod store mounted + creds, same as ops/raw_backfill.sh.
  docker run --rm \
    --network "$NETWORK" \
    --env-file "$ENV_FILE" \
    -v "$STORE_VOLUME":/store \
    -v "$REPO":/app -w /app \
    "$IMAGE" \
    python -m "$@"
}

SUBCMD="${1:-}"
[ -z "$SUBCMD" ] && usage 2
shift || true

case "$SUBCMD" in
  acquire)
    exec "$HERE/raw_backfill.sh" "$@"
    ;;
  materialize)
    [ "$#" -lt 2 ] && { echo "usage: $0 materialize <day> <n> [raw_root]" >&2; exit 2; }
    DAY="$1"; N="$2"; RAW_ROOT="${3:-/store}"
    echo "MATERIALIZE: $N liquid symbols for $DAY from $RAW_ROOT/raw -> features"
    run_module quantlib.features.materialize raw /store "$DAY" "$N" "$RAW_ROOT"
    ;;
  validate)
    [ "$#" -lt 3 ] && { echo "usage: $0 validate <day> <feat_root> <val_root> [--allow-today] [--symbols ...]" >&2; exit 2; }
    echo "VALIDATE: stream-vs-backfill parity -> trust ledger ($*)"
    run_module quantlib.features.validate "$@"
    ;;
  -h|--help|help)
    usage 0
    ;;
  *)
    echo "unknown subcommand: $SUBCMD" >&2
    usage 2
    ;;
esac
