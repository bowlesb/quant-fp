#!/usr/bin/env bash
# PHASE-1 driver for the Within-Day Parity Certifier (docs/WITHIN_DAY_PARITY_CERTIFICATION.md): spot-check
# ONE feature group's live==backfill match on its settled intraday window, for a bounded symbol SAMPLE.
#
# THE MAKE-OR-BREAK (gate-read #4): this shares the box with live capture (fc), so it is HARD resource-
# bounded and YIELDS to fc — it must never dent fc's bar->vector latency:
#   * ONE group, a bounded symbol SAMPLE (default 30, NOT the universe), the SETTLED WINDOW ONLY.
#   * --cpus + --memory caps + a guard-friendly name (quant-backfill* family) so ops/live_monitor.sh's
#     mem/disk guard (#204) PAUSES it under host pressure — NEVER touches fc.
#   * nice/ionice low priority inside the container so the scheduler favours fc.
#   * read-only: reads the store + computes backfill in-memory; writes NOTHING this phase.
#
#   ops/within_day_parity_check.sh --group momentum
#   ops/within_day_parity_check.sh --group momentum --day 2026-06-18 --sample-size 30 --window-minutes 30
#   ops/within_day_parity_check.sh --group momentum --dry-run   # print the docker argv, run nothing
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill-wdpc-check}"

# Hard resource caps — small by design (one group, a 30-symbol sample, a 30-min window is tiny compute).
CPUS="${CPUS:-1.0}"
MEMORY="${MEMORY:-4g}"

GROUP=""
DAY=""
SAMPLE_SIZE="30"
WINDOW_MINUTES="30"
DRY_RUN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --group) GROUP="$2"; shift 2;;
    --day) DAY="$2"; shift 2;;
    --sample-size) SAMPLE_SIZE="$2"; shift 2;;
    --window-minutes) WINDOW_MINUTES="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    *) echo "[wdpc] unknown arg: $1" >&2; exit 2;;
  esac
done

log() { printf '[wdpc] %s\n' "$*" >&2; }

if [ -z "$GROUP" ]; then
  log "ERROR: --group is required (one feature group at a time)."
  exit 2
fi

CMD=(python -m quantlib.features.within_day_parity --feature-root /store --group "$GROUP"
     --sample-size "$SAMPLE_SIZE" --window-minutes "$WINDOW_MINUTES")
[ -n "$DAY" ] && CMD+=(--day "$DAY")

# nice + ionice INSIDE the container so the host scheduler favours fc; the --cpus/--memory caps bound it
# from outside; the guard-name lets live_monitor pause it under pressure.
INNER="nice -n 19 ionice -c3 ${CMD[*]}"

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN — would run guard-named '$CONTAINER' (cpus=$CPUS mem=$MEMORY, nice 19 / ionice idle):"
  log "  docker run --rm --name $CONTAINER --cpus $CPUS --memory $MEMORY -v $STORE_VOLUME:/store -v $REPO:/app -w /app $IMAGE \\"
  log "    sh -c '$INNER'"
  exit 0
fi

log "running $CONTAINER: group=$GROUP day=${DAY:-today} sample=$SAMPLE_SIZE window=${WINDOW_MINUTES}min (cpus=$CPUS mem=$MEMORY, nice 19/ionice idle)"
exec docker run --rm --name "$CONTAINER" \
  --cpus "$CPUS" --memory "$MEMORY" \
  --env-file "$ENV_FILE" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  sh -c "$INNER"
