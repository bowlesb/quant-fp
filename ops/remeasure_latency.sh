#!/usr/bin/env bash
# Re-measure the feature-latency EXPECTATIONS and rewrite docs/feature_latency_expectations.json.
#
# The measurement half of the latency LOOP: re-measure -> write JSON -> the dashboard reads it -> iterate.
# Two callers:
#   * the SCHEDULED recompute (cron, off-hours) — keeps the JSON fresh as the box/codebase drift.
#   * the ONE-SHOT trigger after an optimization lands — so `optimize a group -> ops/remeasure_latency.sh
#     -> JSON + dashboard reflect the win` is one command.
#
# What it does:
#   1. Harvest a recent window of the live crypto-capture container's per-minute compute_ms ON THE HOST
#      (the fp-dev container has no docker socket) into a temp file, passed into the container as the
#      realism cross-check. Best-effort: if crypto-capture is down the JSON still regenerates (status
#      "unavailable").
#   2. Run quantlib.features.latency_expectations --update inside the baked fp-dev image, CPU-CAPPED so the
#      measurement never starves live capture. It drives the per-group isolated compute + the real
#      streaming sim for the e2e block, then writes the JSON deterministically (sorted slowest-first).
#
# CPU-capped + bounded by construction (the sim scale is fixed in the module); safe to run any time, but
# the cron schedules it off-hours anyway. Reads/writes ONLY docs/feature_latency_expectations.json in the
# repo — never touches fc / strategies / the store.
#
#   ops/remeasure_latency.sh                 # full: per-group + e2e + crypto cross-check
#   CPUS=4 ops/remeasure_latency.sh          # tighter cpu cap
#   NO_E2E=1 ops/remeasure_latency.sh        # skip the heavy sim (per-group + crypto only, ~1 min)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CPUS="${CPUS:-8}"
CRYPTO_CONTAINER="${CRYPTO_CONTAINER:-crypto-capture}"
CRYPTO_LOG_TAIL="${CRYPTO_LOG_TAIL:-400}"

EXTRA_ARGS=()
[ -n "${NO_E2E:-}" ] && EXTRA_ARGS+=("--no-e2e")

# 1. Harvest crypto compute_ms on the host (fp-dev can't reach the docker socket).
CRYPTO_FILE="$(mktemp /tmp/crypto_compute_ms.XXXXXX)"
trap 'rm -f "$CRYPTO_FILE"' EXIT
if docker logs --tail "$CRYPTO_LOG_TAIL" "$CRYPTO_CONTAINER" >"$CRYPTO_FILE" 2>&1; then
  echo "harvested $(grep -c compute_ms "$CRYPTO_FILE") crypto compute_ms lines from $CRYPTO_CONTAINER"
else
  echo "WARN: could not read $CRYPTO_CONTAINER logs (crypto cross-check will be 'unavailable')"
  : >"$CRYPTO_FILE"
fi

# 2. Re-measure + rewrite the JSON inside the baked image, cpu-capped.
docker run --rm \
  --cpus="$CPUS" \
  --env-file "$ENV_FILE" \
  ${SOURCE_DATE_EPOCH:+-e "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"} \
  -e "CRYPTO_COMPUTE_MS_FILE=/tmp/crypto_compute_ms.txt" \
  -v "$CRYPTO_FILE":/tmp/crypto_compute_ms.txt:ro \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.features.latency_expectations --update "${EXTRA_ARGS[@]}"

echo "JSON refreshed: $REPO/docs/feature_latency_expectations.json"
