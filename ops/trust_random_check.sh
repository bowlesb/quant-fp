#!/usr/bin/env bash
# Driver for the RANDOM TRUST RE-CHECK (quantlib.features.trust_random_check) — docs/TRUST_REDESIGN.md.
#
# The safety net that makes 1-day trust safe. Picks a random recent CLEAN (already-graded) day, re-runs the
# sweep for it (idempotent), and re-verifies every currently-TRUSTED feature against its per-type threshold.
# A clean-day failure is the ONLY thing that un-trusts a feature (trust is otherwise permanent): it flips the
# feature to NON_TRUSTED, files a parity defect, and appends a feature_trust_check audit row. Reaffirmations
# are recorded too. A graded day whose illiquid raw tail is no longer settled is SKIPPED (the next pool day is
# tried) so a transient never crashes the weekly cron. Runs in the baked fp-dev image against the PRODUCTION
# store volume + DB, creds from .env. NEVER docker-exec's the live feature-computer.
#
# Shares the box with live capture (fc), so it is HARD resource-bounded and YIELDS to fc (the same discipline
# as ops/within_day_parity_check.sh): a guard-friendly name (quant-backfill* family) so ops/live_monitor.sh's
# mem/disk guard PAUSES it under host pressure (never fc), --cpus/--memory caps from outside, and nice/ionice
# low priority inside so the host scheduler favours fc. Scheduled OFF RTH (Sat afternoon) so it cannot dent
# capture; the resource bounds are belt-and-braces.
#
#   ops/trust_random_check.sh                 # a random recent settled graded day
#   DAY=2026-06-18 ops/trust_random_check.sh  # re-check a specific day
#   ops/trust_random_check.sh --dry-run       # print the docker argv + report the cohort that WOULD be checked
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CONTAINER="${CONTAINER:-quant-backfill-trust-random-check}"

# Hard resource caps — the re-check re-materializes ONE settled day; bound it so it can never starve fc.
CPUS="${CPUS:-2.0}"
MEMORY="${MEMORY:-8g}"

DRY_RUN=""
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

log() { printf '[trust-random-check] %s\n' "$*" >&2; }

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

CMD=(python -m quantlib.features.trust_random_check)
[ -n "${DAY:-}" ] && CMD+=("$DAY")
[ -n "$DRY_RUN" ] && CMD+=(--dry-run)

# nice + ionice INSIDE the container so the host scheduler favours fc; the --cpus/--memory caps bound it
# from outside; the guard-name lets live_monitor pause it under host pressure (never fc).
INNER="nice -n 19 ionice -c3 ${CMD[*]}"

if [ -n "$DRY_RUN" ]; then
  log "DRY-RUN — would run guard-named '$CONTAINER' (cpus=$CPUS mem=$MEMORY, nice 19 / ionice idle):"
  log "  docker run --rm --name $CONTAINER --cpus $CPUS --memory $MEMORY -v $STORE_VOLUME:/store -v $REPO:/app -w /app $IMAGE \\"
  log "    sh -c '$INNER'"
  log "(the python --dry-run below reports the candidate days + trusted cohort; it mutates nothing)"
fi

log "running $CONTAINER: day=${DAY:-random-settled} (cpus=$CPUS mem=$MEMORY, nice 19/ionice idle)"
exec docker run --rm --name "$CONTAINER" \
  --network "$NETWORK" \
  --cpus "$CPUS" --memory "$MEMORY" \
  --env-file "$ENV_FILE" \
  -e FP_GIT_COMMIT="$GIT_COMMIT" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  sh -c "$INNER"
