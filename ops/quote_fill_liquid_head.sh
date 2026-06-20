#!/usr/bin/env bash
# Driver for the LIQUID-HEAD quote-fill (docs/TICKER_REPRESENTATION.md): fetch raw QUOTES for the
# most-liquid tradeable names that have a bars tape but ZERO quote tape — chiefly the SPDR sector ETFs
# (XLK/XLE/XLF/XLV/XLI/XLP/XLU/XLY/XLC/XLB/XLRE, ADV rank ~49-674) which power the sector-rotation /
# market-regime conditioners and any quote-spread cost model. The liquid-HEAD sibling of the B4 mid-band
# widener (ops/quote_widen_b4.sh); together they close the quote-breadth gap from both ends.
#
# TWO stages, both in the baked fp-dev image against the PRODUCTION store volume (fp_store_real -> /store)
# with Alpaca creds from .env:
#   1. COMPUTE the liquid-head zero-quote set DETERMINISTICALLY at run time
#      (quantlib.data.liquid_head_quote_gap) — ranks the bars universe by dollar-volume (the same ranker
#      the deep-backfill uses), takes the head slice, subtracts names already in the quotes manifest.
#      No stale hardcoded list, so a re-run after a partial fill targets only what is still missing.
#   2. FETCH quotes for exactly those names over the full quote span via quantlib.data.raw_backfill WINDOW
#      mode (--symbols ... --start ... --end ...). Idempotent (manifest-skips already-fetched symbol-days),
#      memory-bounded (--processes 1, --quotes-chunk-days 1), budget-capped.
#
# The fetch container is DETACHED + NAMED `quant-backfill-quotes-sectoretf` so ops/live_monitor.sh's
# generalized mem/disk guard (PR #204: pattern quant-backfill*/*-backfill) PAUSES it under host pressure to
# protect live capture — NEVER touches fc. ONE-AT-A-TIME: do not launch while another heavy backfill is up.
#
#   ops/quote_fill_liquid_head.sh            # compute + launch the detached fetch (default span/budget)
#   ops/quote_fill_liquid_head.sh --dry-run  # compute + print the symbol set + the docker argv, launch NOTHING
#   START=2024-12-12 END=2026-06-18 BUDGET_TB=0.02 ops/quote_fill_liquid_head.sh   # override span/budget
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill-quotes-sectoretf}"

# Full quote span to date — the liquid-head names have NO quotes, so the whole window is fetchable.
# Idempotent resume means re-running after a partial fill only fetches what is still missing.
START="${START:-2024-12-12}"
END="${END:-2026-06-18}"
HEAD_RANK="${HEAD_RANK:-1000}"
BUDGET_TB="${BUDGET_TB:-0.02}"        # 20GB cap — a handful of liquid-head names, comfortably under this
# WINDOW mode slices quote_symbols = ranked[:top_quotes]; we pass exactly the head set as the universe,
# so a large top-quotes keeps them all. top_trades 0 => quotes only.
TOP_QUOTES="${TOP_QUOTES:-5000}"

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '[quote_fill_liquid_head] %s\n' "$*" >&2; }

# Refuse to stack on another running heavy backfill (one-at-a-time; protects Monday capture headroom).
RUNNING_BACKFILLS="$(docker ps --format '{{.Names}}' 2>/dev/null \
  | grep -E 'quant-backfill|-backfill' | grep -v "^${CONTAINER}\$" || true)"
if [ -n "$RUNNING_BACKFILLS" ] && [ -z "$DRY_RUN" ]; then
  log "REFUSING to launch — another backfill container is running (one-at-a-time):"
  log "$RUNNING_BACKFILLS"
  log "re-run after it finishes, or use --dry-run to preview."
  exit 1
fi

log "STAGE 1/2 COMPUTE: deterministic liquid-head zero-quote set (ADV rank < $HEAD_RANK, minus already-quoted)"
SYMBOLS="$(docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.data.liquid_head_quote_gap --store /store --end "$END" --head-rank "$HEAD_RANK")"

if [ -z "$SYMBOLS" ]; then
  log "EMPTY liquid-head zero-quote set — nothing to fill (all head names already have quotes, or bars manifest empty)."
  exit 0
fi
N="$(printf '%s' "$SYMBOLS" | tr ',' '\n' | grep -c .)"
log "computed $N liquid-head zero-quote target symbols: $SYMBOLS"

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

if [ -n "$DRY_RUN" ]; then
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
