#!/usr/bin/env bash
# Driver for the RANDOM TRUST RE-CHECK (quantlib.features.trust_random_check) — docs/TRUST_REDESIGN.md.
#
# The safety net that makes 1-day trust safe. Picks a random recent CLEAN (already-graded) day, re-runs the
# sweep for it (idempotent), and re-verifies every currently-TRUSTED feature against its per-type threshold.
# A clean-day failure is the ONLY thing that un-trusts a feature (trust is otherwise permanent): it flips the
# feature to NON_TRUSTED, files a parity defect, and appends a feature_trust_check audit row. Reaffirmations
# are recorded too. Runs in the baked fp-dev image against the PRODUCTION store volume + DB, creds from .env.
# NEVER docker-exec's the live feature-computer.
#
#   ops/trust_random_check.sh                 # a random recent graded day
#   DAY=2026-06-18 ops/trust_random_check.sh  # re-check a specific day
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"

ARGS=()
[ -n "${DAY:-}" ] && ARGS+=("$DAY")

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e FP_GIT_COMMIT="$GIT_COMMIT" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.features.trust_random_check "${ARGS[@]}"
