#!/usr/bin/env bash
# Driver for the #208 B4 quote-WIDENING tranche (docs/TICKER_REPRESENTATION.md): fetch raw QUOTES for the
# mid-liquidity (ADV rank 2000-4000) tradeable names that have bars + trades but ZERO quote tape — a cheap,
# bounded breadth win that lifts B4 quote coverage from ~95% toward 100%.
#
# TWO stages, both in the baked fp-dev image against the PRODUCTION store volume (fp_store_real -> /store)
# with Alpaca creds from .env:
#   1. COMPUTE the B4 zero-quote set DETERMINISTICALLY at run time (quantlib.data.b4_quote_widen) — ranks the
#      bars universe by dollar-volume (the same ranker the deep-backfill uses), slices the B4 band, subtracts
#      names already in the quotes manifest. No stale hardcoded list.
#   2. FETCH quotes for exactly those names over the full quote span via quantlib.data.raw_backfill WINDOW
#      mode (--symbols ... --start ... --end ...). Idempotent (manifest-skips already-fetched symbol-days),
#      memory-bounded (--processes 1, --quotes-chunk-days 1), budget-capped.
#
# The fetch container is DETACHED + NAMED `quant-backfill-quotes-b4widen` so ops/live_monitor.sh's generalized
# mem/disk guard (PR #204: pattern quant-backfill*/*-backfill) PAUSES it under host pressure to protect live
# capture — NEVER touches fc. ONE-AT-A-TIME: do not launch while another heavy backfill container is running.
#
#   ops/quote_widen_b4.sh            # compute + launch the detached fetch (default span/budget)
#   ops/quote_widen_b4.sh --dry-run  # compute + print the symbol set + the docker argv, launch NOTHING
#   START=2024-12-12 END=2026-06-18 BUDGET_TB=0.05 ops/quote_widen_b4.sh   # override span/budget
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill-quotes-b4widen}"

# Full quote span to date — the B4 names have NO quotes, so the whole window is fetchable. Idempotent resume
# means re-running after a partial fill only fetches what is still missing.
START="${START:-2024-12-12}"
END="${END:-2026-06-18}"
BUDGET_TB="${BUDGET_TB:-0.05}"        # 50GB cap — the B4 set is ~100 names, comfortably under this
# Keep all passed symbols: WINDOW mode slices quote_symbols = ranked[:top_quotes], and we pass exactly the
# B4 set as the universe, so a large top-quotes keeps them all. top_trades 0 => quotes only.
TOP_QUOTES="${TOP_QUOTES:-5000}"

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '[quote_widen_b4] %s\n' "$*" >&2; }

# Refuse to stack on another running heavy backfill (one-at-a-time; protects Monday capture headroom).
RUNNING_BACKFILLS="$(docker ps --format '{{.Names}}' 2>/dev/null \
  | grep -E 'quant-backfill|-backfill' | grep -v "^${CONTAINER}\$" || true)"
if [ -n "$RUNNING_BACKFILLS" ] && [ -z "$DRY_RUN" ]; then
  log "REFUSING to launch — another backfill container is running (one-at-a-time):"
  log "$RUNNING_BACKFILLS"
  log "re-run after it finishes, or use --dry-run to preview."
  exit 1
fi

log "STAGE 1/2 COMPUTE: deterministic B4 zero-quote set (ADV rank 2000-4000, minus already-quoted)"
SYMBOLS="$(docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.data.b4_quote_widen --store /store --end "$END")"

if [ -z "$SYMBOLS" ]; then
  log "EMPTY B4 zero-quote set — nothing to widen (all B4 names already have quotes, or bars manifest empty)."
  exit 0
fi
N="$(printf '%s' "$SYMBOLS" | tr ',' '\n' | grep -c .)"
log "computed $N B4 zero-quote target symbols"

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN — symbols: $SYMBOLS"
  log "DRY-RUN — would launch detached container '$CONTAINER':"
  log "  raw_backfill --store /store --symbols <$N syms> --start $START --end $END \\"
  log "    --top-quotes $TOP_QUOTES --top-trades 0 --budget-tb $BUDGET_TB --processes 1 --quotes-chunk-days 1"
  exit 0
fi

log "STAGE 2/2 FETCH: detached, guard-named '$CONTAINER', $N syms x $START..$END (quotes only, bounded)"
docker run -d --name "$CONTAINER" \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e FP_GIT_COMMIT="$GIT_COMMIT" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.data.raw_backfill \
    --store /store \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --top-quotes "$TOP_QUOTES" --top-trades 0 \
    --budget-tb "$BUDGET_TB" \
    --processes 1 --quotes-chunk-days 1

log "launched $CONTAINER — monitor with: docker logs -f $CONTAINER ; reconcile (docker rm) on exit."
