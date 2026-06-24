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
#   2. Run quantlib.features.latency_expectations --update inside the baked fp-dev image, PINNED to a set of
#      genuinely-idle cores (--cpuset-cpus) so the measurement never starves live capture AND is not itself
#      contended by it. It drives the per-group isolated compute + the real streaming sim for the e2e block,
#      then writes the JSON deterministically (sorted slowest-first).
#
# Why PIN, not just cap: this box is a busy production machine (live fc + strategies + crypto-capture + other
# work). A plain --cpus cap shares cores with the live stack, so per-group p95/p99 tails swing wildly run to
# run (a single contended rep among the reps spikes a group's p99) — the noisy-tail "implausible armed
# latency" artifact. Pinning to idle cores gives stable, reproducible numbers. The 1-min load AVERAGE is a
# misleading idleness gate (it can read high from I/O wait / brief bursts while most cores sit idle), so the
# picker reads per-core utilization from /proc/stat deltas directly. GRACEFUL FALLBACK: if it cannot find
# enough idle cores it falls back to --cpus and logs a warning — the cron regen must never hard-fail.
#
# Bounded by construction (the sim scale is fixed in the module); safe to run any time, but the cron
# schedules it off-hours anyway. Reads/writes ONLY docs/feature_latency_expectations.json in the repo —
# never touches fc / strategies / the store.
#
#   ops/remeasure_latency.sh                 # full: per-group + e2e + crypto cross-check
#   CPUS=4 ops/remeasure_latency.sh          # fewer cores (pins 4 idle, or --cpus=4 fallback)
#   PIN_CORES=0 ops/remeasure_latency.sh     # disable pinning, force the plain --cpus cap
#   IDLE_MIN_PCT=70 ops/remeasure_latency.sh # a core counts as idle at >=70% idle (default 80)
#   NO_E2E=1 ops/remeasure_latency.sh        # skip the heavy sim (per-group + crypto only, ~1 min)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
CPUS="${CPUS:-8}"
PIN_CORES="${PIN_CORES:-1}"      # 1 = pin to idle cores (default); 0 = plain --cpus cap
IDLE_MIN_PCT="${IDLE_MIN_PCT:-80}"  # a core must be >= this % idle over the sample window to be eligible
CRYPTO_CONTAINER="${CRYPTO_CONTAINER:-crypto-capture}"
CRYPTO_LOG_TAIL="${CRYPTO_LOG_TAIL:-400}"

EXTRA_ARGS=()
[ -n "${NO_E2E:-}" ] && EXTRA_ARGS+=("--no-e2e")

# Pick up to CPUS genuinely-idle core indices (>= IDLE_MIN_PCT idle over a 1s /proc/stat sample), most-idle
# first, as a comma-separated list on stdout. Empty output => could not find enough idle cores (caller falls
# back to --cpus). Pure-stdlib python so it runs on the host without extra deps; any failure prints nothing.
pick_idle_cores() {
  python3 - "$CPUS" "$IDLE_MIN_PCT" <<'PY' 2>/dev/null || true
import sys, time

def snapshot() -> dict[str, tuple[int, int]]:
    cores: dict[str, tuple[int, int]] = {}
    with open("/proc/stat") as stat:
        for line in stat:
            if line.startswith("cpu") and line[3:4].isdigit():
                parts = line.split()
                vals = list(map(int, parts[1:]))
                cores[parts[0]] = (sum(vals), vals[3] + vals[4])  # (total jiffies, idle+iowait)
    return cores

want = int(sys.argv[1])
min_pct = float(sys.argv[2])
before = snapshot()
time.sleep(1)
after = snapshot()
idle_pct = []
for core, (total0, idle0) in before.items():
    total1, idle1 = after[core]
    delta_total = total1 - total0
    if delta_total > 0:
        idle_pct.append(((idle1 - idle0) / delta_total * 100.0, core[3:]))
idle_pct.sort(reverse=True)
chosen = [core for pct, core in idle_pct if pct >= min_pct][:want]
print(",".join(chosen))
PY
}

CPU_FLAGS=(--cpus="$CPUS")
if [ "$PIN_CORES" != "0" ]; then
  IDLE_CPUS="$(pick_idle_cores)"
  n_idle=0
  [ -n "$IDLE_CPUS" ] && n_idle=$(($(grep -o , <<<"$IDLE_CPUS" | wc -l) + 1))
  if [ "$n_idle" -ge "$CPUS" ]; then
    CPU_FLAGS=(--cpuset-cpus="$IDLE_CPUS")
    echo "pinning measurement to $n_idle idle cores: $IDLE_CPUS (>= ${IDLE_MIN_PCT}% idle)"
  else
    echo "WARN: only $n_idle core(s) >= ${IDLE_MIN_PCT}% idle (< CPUS=$CPUS) — falling back to --cpus=$CPUS" \
         "(numbers may carry contention noise in the tails)"
  fi
fi

# 1. Harvest crypto compute_ms on the host (fp-dev can't reach the docker socket).
CRYPTO_FILE="$(mktemp /tmp/crypto_compute_ms.XXXXXX)"
trap 'rm -f "$CRYPTO_FILE"' EXIT
if docker logs --tail "$CRYPTO_LOG_TAIL" "$CRYPTO_CONTAINER" >"$CRYPTO_FILE" 2>&1; then
  echo "harvested $(grep -c compute_ms "$CRYPTO_FILE") crypto compute_ms lines from $CRYPTO_CONTAINER"
else
  echo "WARN: could not read $CRYPTO_CONTAINER logs (crypto cross-check will be 'unavailable')"
  : >"$CRYPTO_FILE"
fi

# 2. Re-measure + rewrite the JSON inside the baked image, pinned to idle cores (or --cpus fallback).
docker run --rm \
  "${CPU_FLAGS[@]}" \
  --env-file "$ENV_FILE" \
  ${SOURCE_DATE_EPOCH:+-e "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"} \
  -e "CRYPTO_COMPUTE_MS_FILE=/tmp/crypto_compute_ms.txt" \
  -v "$CRYPTO_FILE":/tmp/crypto_compute_ms.txt:ro \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.features.latency_expectations --update "${EXTRA_ARGS[@]}"

echo "JSON refreshed: $REPO/docs/feature_latency_expectations.json"
