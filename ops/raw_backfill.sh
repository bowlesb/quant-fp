#!/usr/bin/env bash
# Driver for the shared /store/raw 6-month raw bars/trades/quotes backfill.
#
# Runs quantlib.data.raw_backfill inside the baked fp-dev image with the PRODUCTION store volume
# (fp_store_real -> /store) and Alpaca creds from .env. The job is RESUMABLE + idempotent: a re-run
# skips symbol-days already in the per-tier manifest, so this script is safe to re-launch after an
# interruption. The LEAD kicks off the full run; do NOT run the full 6-month job blindly from a
# subagent (it is long). Use SAMPLE mode for evidence:
#
#   ops/raw_backfill.sh sample                 # AAPL,SPY,NVDA x 2 recent trading days (evidence)
#   ops/raw_backfill.sh daily                  # full universe + SPY/QQQ, last DAYS settled day(s) (nightly)
#   ops/raw_backfill.sh full                   # 6mo, top-1500 trades / top-300 quotes, 1.8TB budget
#   TOP_TRADES=2000 TOP_QUOTES=400 ops/raw_backfill.sh full   # override tier widths
#   DAYS=2 ops/raw_backfill.sh daily           # acquire the last 2 settled days (catch up a missed night)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"

MONTHS="${MONTHS:-6}"
TOP_TRADES="${TOP_TRADES:-1500}"
TOP_QUOTES="${TOP_QUOTES:-300}"
BUDGET_TB="${BUDGET_TB:-1.8}"
DAYS="${DAYS:-1}"

MODE="${1:-full}"

run_job() {
  docker run --rm \
    --network "$NETWORK" \
    --env-file "$ENV_FILE" \
    -v "$STORE_VOLUME":/store \
    -v "$REPO":/app -w /app \
    "$IMAGE" \
    python -m quantlib.data.raw_backfill "$@"
}

case "$MODE" in
  sample)
    SYMBOLS="${SYMBOLS:-AAPL,SPY,NVDA}"
    DAYS="${DAYS:-2}"
    echo "SAMPLE: $SYMBOLS x $DAYS recent trading days -> /store/raw"
    run_job --store /store --symbols "$SYMBOLS" --days "$DAYS" \
      --top-trades "$TOP_TRADES" --top-quotes "$TOP_QUOTES" --budget-tb "$BUDGET_TB"
    ;;
  daily)
    echo "DAILY: full universe + SPY/QQQ, last ${DAYS} settled trading day(s) -> /store/raw (idempotent)"
    run_job --store /store --days "$DAYS" \
      --top-trades "$TOP_TRADES" --top-quotes "$TOP_QUOTES" --budget-tb "$BUDGET_TB"
    ;;
  full)
    echo "FULL: ${MONTHS}mo, trades top-${TOP_TRADES}, quotes top-${TOP_QUOTES}, budget ${BUDGET_TB}TB"
    run_job --store /store --months "$MONTHS" \
      --top-trades "$TOP_TRADES" --top-quotes "$TOP_QUOTES" --budget-tb "$BUDGET_TB"
    ;;
  *)
    echo "usage: $0 [sample|daily|full]" >&2
    exit 2
    ;;
esac
