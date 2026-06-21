#!/usr/bin/env bash
# Driver for the BREADTH-AT-DEPTH quote backfill (docs/TICKER_REPRESENTATION.md): extend the broad
# ~3,950-symbol quote breadth BACKWARD in time. The broad breadth onset is 2026-03-18; before it only a
# ~530-symbol liquid HEAD has quotes (reaching into 2024-12). This fetches quotes for the broad-but-non-head
# names over an earlier target window so the deep quote panel has full breadth at depth — the deep-history
# foundation for cheap feature invention (NON-blocking; cost-model regime robustness, not a fresh alpha edge).
#
# TWO stages, both in the baked fp-dev image against the PRODUCTION store volume (fp_store_real -> /store)
# with Alpaca creds from .env:
#   1. COMPUTE the breadth-depth target set DETERMINISTICALLY at run time
#      (quantlib.data.quote_breadth_depth_gap): the broad-era names (real tape on a settled broad ref date)
#      missing quote coverage before the window start. No stale hardcoded list.
#   2. FETCH quotes for exactly those names over [START, END] via quantlib.data.raw_backfill WINDOW mode.
#      Idempotent (manifest-skips already-fetched symbol-days), memory-bounded (--processes 1,
#      --quotes-chunk-days 1), budget-capped. --top-trades 0 => quotes only; the bars step is a no-op
#      (breadth names already have bars 18mo back) but its budget guard may STOP harmlessly on a tight
#      --budget-tb — quotes still proceed.
#
# The fetch container is DETACHED + NAMED `quant-backfill` (the exact name ops/live_monitor.sh's mem/disk
# guard pauses under host pressure to protect live capture — NEVER touches fc). ONE-AT-A-TIME by default:
# refuses to stack on another running backfill. For the full multi-hour fill the Lead may run several
# DISJOINT-shard containers in parallel (CONTAINER=quant-backfill-q1-shardN, SYMBOLS_FILE per shard).
#
#   ops/quote_breadth_depth_fill.sh                          # compute + launch (default Q1 window)
#   ops/quote_breadth_depth_fill.sh --dry-run                # compute + print set + docker argv, launch nothing
#   START=2025-10-01 END=2026-03-17 ops/quote_breadth_depth_fill.sh   # override the target window
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill}"

# Target window: the head-only stretch we want to widen to broad. Default = Q1-2026 (the first backfill
# slice the DataIntegrity scope recommends); override to 2025-10-01 for the wider fill. The broad onset is
# 2026-03-18, so an END at/before 2026-03-17 fills purely pre-onset dates (idempotent past it regardless).
START="${START:-2026-01-02}"
END="${END:-2026-03-17}"
BROAD_REF_DATE="${BROAD_REF_DATE:-2026-03-23}"
BUDGET_TB="${BUDGET_TB:-0.15}"          # 150GB cap — comfortably over the ~25-56GB scoped fill
# WINDOW mode slices quote_symbols = ranked[:top_quotes]; we pass exactly the target set, so a large
# top-quotes keeps them all. top_trades 0 => quotes only.
TOP_QUOTES="${TOP_QUOTES:-8000}"
CPUS="${CPUS:-4}"
MEMORY="${MEMORY:-12g}"

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '[quote_breadth_depth_fill] %s\n' "$*" >&2; }

# Refuse to stack on another running heavy backfill (one-at-a-time; protects live capture headroom).
RUNNING_BACKFILLS="$(docker ps --format '{{.Names}}' 2>/dev/null \
  | grep -E 'quant-backfill|-backfill' | grep -v "^${CONTAINER}\$" || true)"
if [ -n "$RUNNING_BACKFILLS" ] && [ -z "$DRY_RUN" ]; then
  log "REFUSING to launch — another backfill container is running (one-at-a-time):"
  log "$RUNNING_BACKFILLS"
  log "re-run after it finishes, or use --dry-run to preview."
  exit 1
fi

log "STAGE 1/2 COMPUTE: broad breadth-depth target set (broad ref $BROAD_REF_DATE, missing < $START)"
SYMBOLS="$(docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e PYTHONPATH=/app \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.data.quote_breadth_depth_gap --store /store \
    --window-start "$START" --broad-ref-date "$BROAD_REF_DATE")"

if [ -z "$SYMBOLS" ]; then
  log "EMPTY breadth-depth set — every broad name already reaches < $START (nothing to fill)."
  exit 0
fi
N="$(printf '%s' "$SYMBOLS" | tr ',' '\n' | grep -c .)"
log "computed $N breadth-depth target symbols"

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN — $N symbols (first 30): $(printf '%s' "$SYMBOLS" | tr ',' '\n' | head -30 | tr '\n' ',')"
  log "DRY-RUN — would launch detached container '$CONTAINER':"
  log "  raw_backfill --store /store --symbols <$N syms> --start $START --end $END \\"
  log "    --top-quotes $TOP_QUOTES --top-trades 0 --budget-tb $BUDGET_TB --processes 1 --quotes-chunk-days 1"
  exit 0
fi

log "STAGE 2/2 FETCH: detached, guard-named '$CONTAINER', $N syms x $START..$END (quotes only, bounded)"
docker run -d --name "$CONTAINER" \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  --cpus "$CPUS" --memory "$MEMORY" \
  -e PYTHONPATH=/app \
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

log "launched $CONTAINER — monitor: docker logs -f $CONTAINER ; reconcile (docker rm) on exit."
