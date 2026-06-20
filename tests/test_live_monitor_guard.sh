#!/usr/bin/env bash
# Test for the generalized mem/disk guard in ops/live_monitor.sh: the guard must pause EVERY non-protected
# heavy/backfill job (matched by name pattern, not one hardcoded name — the rawdepth-pilot-quotes guard-blind
# OOM gap, 2026-06-19) while NEVER pausing anything in the PROTECTED set (capture/strategies/store/infra).
#
# It STUBS `docker` (a function shadowing the binary) so `docker ps` returns a fixed container set and
# `docker stop` records names instead of touching anything, then sources ONLY the guard's pure functions
# (PROTECTED_SET / JOB_PATTERNS / is_protected / pause_jobs_under_pressure, extracted from the script between
# sentinel markers) and asserts exactly which containers it would stop. No real docker, no real stops.
#
#   tests/test_live_monitor_guard.sh   # exits 0 on pass, non-zero + diff on failure
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../ops/live_monitor.sh"

# The fixed running-container set: the protected live apparatus + realistic ad-hoc job names (incl the exact
# rawdepth-pilot-quotes that used to be guard-invisible) + neutral non-job containers.
RUNNING_CONTAINERS="feature-computer
smoke-strategy
reversion-strategy
overnight-beta-strategy
crypto-capture
quant-redis
quant-timescaledb-1
quant-dashboard-1
quant-grafana-1
quant-prometheus-1
quant-edgar-1
quant-backfill
rawdepth-pilot-quotes
dia-oflow-bf
deepfactors_full
some-quote-backfill
nightly-bf
claude-reviewer-web
festive_jang
cool_brattain"

STOPPED_LOG="$(mktemp)"
trap 'rm -f "$STOPPED_LOG"' EXIT

# Stub docker: `docker ps --format '{{.Names}}'` -> the fixed set; `docker stop NAME` -> record NAME; else no-op.
docker() {
  case "$1" in
    ps) printf '%s\n' "$RUNNING_CONTAINERS" ;;
    stop) shift; printf '%s\n' "$1" >> "$STOPPED_LOG"; return 0 ;;
    *) return 0 ;;
  esac
}

# note() is called by pause_jobs_under_pressure; capture its tags harmlessly.
note() { :; }

# Source ONLY the guard functions (between the sentinel markers) so sourcing does not run the whole monitor.
eval "$(awk '/# >>> GUARD-FUNCTIONS-BEGIN/{f=1;next} /# <<< GUARD-FUNCTIONS-END/{f=0} f' "$SCRIPT")"

pause_jobs_under_pressure "test" >/dev/null

STOPPED="$(sort "$STOPPED_LOG")"
EXPECTED="$(printf '%s\n' quant-backfill rawdepth-pilot-quotes dia-oflow-bf deepfactors_full some-quote-backfill nightly-bf | sort)"

fail=0
# Every protected name must NOT be in the stopped set.
for protected in feature-computer smoke-strategy reversion-strategy overnight-beta-strategy crypto-capture \
                 quant-redis quant-timescaledb-1 quant-dashboard-1 quant-grafana-1 quant-prometheus-1 quant-edgar-1; do
  if printf '%s\n' "$STOPPED" | grep -qx "$protected"; then
    echo "FAIL: protected container was paused: $protected"; fail=1
  fi
done

# The stopped set must equal exactly the expected job set.
if [ "$STOPPED" != "$EXPECTED" ]; then
  echo "FAIL: paused set != expected"
  echo "--- paused ---"; printf '%s\n' "$STOPPED"
  echo "--- expected ---"; printf '%s\n' "$EXPECTED"
  fail=1
fi

# Neutral non-job containers must NOT be paused.
for neutral in claude-reviewer-web festive_jang cool_brattain; do
  if printf '%s\n' "$STOPPED" | grep -qx "$neutral"; then
    echo "FAIL: neutral container was paused: $neutral"; fail=1
  fi
done

if [ "$fail" = 0 ]; then
  echo "PASS: paused exactly the $(printf '%s\n' "$EXPECTED" | wc -l | tr -d ' ') non-protected jobs; all protected + neutral containers untouched"
fi
exit "$fail"
