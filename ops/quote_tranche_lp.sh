#!/usr/bin/env bash
# Driver for the #208 NEXT quote tranche (docs/TICKER_REPRESENTATION.md): the tight-spread (1-5bps)
# liquid-head mega/large-cap LP-headroom names — the realistic liquidity-provision / spread-capture surface.
# Sibling of ops/quote_widen_b4.sh; where B4 WIDENED breadth into the mid-liquidity tail, this DEEPENS the
# tight-spread head (the quote tape an LP / market-making hunt needs).
#
# TWO stages, both in the baked fp-dev image against the PRODUCTION store volume (fp_store_real -> /store)
# with Alpaca creds from .env:
#   1. COMPUTE the tranche DETERMINISTICALLY at run time (quantlib.data.next_quote_tranche) — ranks the bars
#      universe by dollar-volume (the same shared ranker), takes the liquid head, MEASURES median spread (bps)
#      + LP-headroom from the on-disk quote tape (universe_membership.median_spread_bps is NULL, so spread is
#      measured from the deep-quote panel itself), keeps the 1-5bps band, ranks deepest-headroom first. No
#      stale hardcoded list.
#   2. FETCH quotes for exactly those names over the span via quantlib.data.raw_backfill WINDOW mode. These
#      names ALREADY have quote coverage (they had to, to be measured), so this is an idempotent REFRESH that
#      only pulls symbol-days still missing (e.g. brings the tape current to the latest settled day). Memory-
#      bounded (--processes 1, --quotes-chunk-days 1), budget-capped.
#
# The fetch container is DETACHED + NAMED `quant-backfill-quotes-lptranche` so ops/live_monitor.sh's mem/disk
# guard (pattern quant-backfill*/*-backfill) PAUSES it under host pressure to protect live capture — NEVER
# touches fc. ONE-AT-A-TIME: do not launch while another heavy backfill container is running.
#
#   ops/quote_tranche_lp.sh             # compute + launch the detached refresh (default span/budget)
#   ops/quote_tranche_lp.sh --dry-run   # compute + print the ranked tranche + the docker argv, launch NOTHING
#   HEAD_RANK_END=1000 START=2024-12-12 END=2026-06-18 ops/quote_tranche_lp.sh   # override head/span
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill-quotes-lptranche}"

START="${START:-2024-12-12}"
END="${END:-2026-06-18}"
BUDGET_TB="${BUDGET_TB:-0.10}"        # 100GB cap — tight-spread mega-caps quote heavily; refresh is bounded
TOP_QUOTES="${TOP_QUOTES:-5000}"      # WINDOW mode slices ranked[:top_quotes]; large keeps all passed names
HEAD_RANK_END="${HEAD_RANK_END:-1000}"

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '[quote_tranche_lp] %s\n' "$*" >&2; }

# Refuse to stack on another running heavy backfill (one-at-a-time; protects live-capture headroom).
RUNNING_BACKFILLS="$(docker ps --format '{{.Names}}' 2>/dev/null \
  | grep -E 'quant-backfill|-backfill' | grep -v "^${CONTAINER}\$" || true)"
if [ -n "$RUNNING_BACKFILLS" ] && [ -z "$DRY_RUN" ]; then
  log "REFUSING to launch — another backfill container is running (one-at-a-time):"
  log "$RUNNING_BACKFILLS"
  log "re-run after it finishes, or use --dry-run to preview."
  exit 1
fi

log "STAGE 1/2 COMPUTE: tight-spread 1-5bps liquid-head LP tranche (measured from the deep-quote panel)"
SYMBOLS="$(docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.data.next_quote_tranche --store /store --end "$END" --head-rank-end "$HEAD_RANK_END")"

if [ -z "$SYMBOLS" ]; then
  log "EMPTY tranche — no liquid-head name measured a 1-5bps median spread (or bars/quotes manifest empty)."
  exit 0
fi
N="$(printf '%s' "$SYMBOLS" | tr ',' '\n' | grep -c .)"
log "computed $N tight-spread LP target symbols (ranked deepest-headroom first)"

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN — tranche: $SYMBOLS"
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
