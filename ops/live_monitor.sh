#!/usr/bin/env bash
# Independent auto-heal monitor for the LIVE trading apparatus. Runs from cron every few minutes,
# survives regardless of any interactive/agent session. It (a) restarts critical containers that have
# EXITED (the OOM/crash case), (b) guards host memory + store disk by pausing EVERY non-protected
# heavy/backfill job (matched by name pattern, not one hardcoded name) before it can starve the live
# capture, and (c) appends a JSON status line for the audit trail. It is deliberately conservative:
# it only restarts containers that are NOT running (never restart-loops a healthy-but-warming capture)
# and it NEVER pauses anything in the PROTECTED set (capture/strategies/store/infra), leaving judgement
# calls (stalls, latency, bet-logic bugs) to the active oversight loop.
set -uo pipefail

LOG_DIR=/home/ben/.quant-ops
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/live_monitor.jsonl"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ACTIONS=""

note() { ACTIONS="${ACTIONS:+$ACTIONS,}\"$1\""; }

state_of() { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo missing; }

# >>> GUARD-FUNCTIONS-BEGIN (sourced verbatim by tests/test_live_monitor_guard.sh — keep self-contained)
# The PROTECTED set is the live trading apparatus + its infra: the mem/disk guard must NEVER stop any of
# these (stopping capture/strategies/store to "save memory" is the very harm the guard exists to prevent).
# This allowlist is the hard safety guarantee — the pause logic below pauses ONLY non-protected job
# containers, so adding a new live/infra service here makes it guard-safe immediately.
PROTECTED_SET="feature-computer smoke-strategy reversion-strategy overnight-beta-strategy crypto-capture quant-redis quant-timescaledb-1 quant-dashboard-1 quant-grafana-1 quant-prometheus-1 quant-edgar-1"

is_protected() {  # true if container name $1 is in the protected allowlist (never pause it)
  case " $PROTECTED_SET " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Heavy/backfill JOB name patterns the guard MAY pause under pressure. Broad on purpose so an ad-hoc-named
# job can't starve capture invisibly (the rawdepth-pilot-quotes guard-blind OOM, 2026-06-19 — it was named
# off-pattern from the single hardcoded "quant-backfill" the guard used to check, so it was never paused).
# Anything in PROTECTED is excluded regardless of pattern, so widening these can never endanger the live
# apparatus. Extend as new job-naming conventions appear.
JOB_PATTERNS='^(quant-backfill|dia-|rawdepth-|deepfactor|.*-backfill$|.*-bf$|.*-backfill-)'

# Stop every RUNNING, non-protected container whose NAME matches a job pattern. $1 = reason tag for the
# audit note. Idempotent (a re-run finds the stopped job gone from `docker ps`, so it is a no-op) and
# fail-safe (a docker-stop error is swallowed; the guard still writes its snapshot). Prints the count paused.
pause_jobs_under_pressure() {
  local reason="$1" paused=0 name
  while read -r name; do
    [ -z "$name" ] && continue
    is_protected "$name" && continue          # belt-and-suspenders: pattern should not catch protected, but enforce it
    if docker stop "$name" >/dev/null 2>&1; then note "paused:${name}:${reason}"; paused=$((paused + 1)); fi
  done < <(docker ps --format '{{.Names}}' 2>/dev/null | grep -E "$JOB_PATTERNS")
  printf '%s' "$paused"
}
# <<< GUARD-FUNCTIONS-END

# --- 1. critical infra: restart anything not running (same container preserves config/env) ---
# feature-computer's session date is a hardcoded launch arg (see docs/OPERATIONS.md). If it merely STOPPED
# the same container is the current-day one (the pre-market relaunch cron keeps its date fresh), so a
# `docker start` is correct. But if it is MISSING (e.g. nightly_relaunch did rm -f then failed to recreate),
# `docker start` cannot bring back a removed container — so REBUILD it for today via nightly_relaunch. This
# is the recovery path for the one destructive cron (the rm-f-then-recreate guardrail).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for svc in quant-redis quant-timescaledb-1 smoke-strategy reversion-strategy overnight-beta-strategy feature-computer; do
  st=$(state_of "$svc")
  [ "$st" = "running" ] && continue
  if [ "$svc" = "feature-computer" ] && [ "$st" = "missing" ]; then
    if (cd "$REPO_DIR" && ENV_FILE="$REPO_DIR/.env" STORE_ROOT=/store ops/nightly_relaunch.sh "$(date -u +%F)" >/dev/null 2>&1); then
      note "relaunched:feature-computer(was:missing)"
    else
      note "FAILED-relaunch:feature-computer(was:missing)"
    fi
    continue
  fi
  if docker start "$svc" >/dev/null 2>&1; then note "restarted:$svc(was:$st)"; else note "FAILED-restart:$svc(was:$st)"; fi
done

# --- 2. host-memory guard: if free RAM < 8%, pause EVERY non-protected heavy/backfill job (not just one
#        hardcoded name) so no job — however named — can OOM-starve the live capture. ---
MEM_FREE_PCT=$(free | awk '/Mem:/{printf "%d", ($7/$2)*100}')
if [ "${MEM_FREE_PCT:-100}" -lt 8 ]; then
  pause_jobs_under_pressure "lowmem(${MEM_FREE_PCT}%free)" >/dev/null
fi

# --- 3. disk guard: if the store partition is > 92% full, pause EVERY non-protected heavy/backfill job
#        (they write the most data) so the store can't fill out from under the live capture. ---
DISK_PCT=$(df --output=pcent /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9')
if [ "${DISK_PCT:-0}" -gt 92 ]; then
  pause_jobs_under_pressure "lowdisk(${DISK_PCT}%used)" >/dev/null
fi

# --- 4. quick observability snapshot for the audit trail ---
BETS=$(docker exec quant-timescaledb-1 psql -U quant -d quant -tA -c \
  "select count(*)||'/'||count(*) filter (where status='closed') from strat_smoke.bets" 2>/dev/null | tail -1)
FV_STREAMS=$(docker exec quant-redis redis-cli --scan --pattern 'fv:*' 2>/dev/null | wc -l | tr -d ' ')
# Count of running non-protected jobs the guard now WATCHES (not just the legacy hardcoded name) — so the
# audit trail shows the full pausable set, the legibility the old single-name "backfill" field lacked.
JOBS_RUNNING=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -Ec "$JOB_PATTERNS")

printf '{"ts":"%s","containers":{"fc":"%s","smoke":"%s","redis":"%s","db":"%s","backfill":"%s"},"mem_free_pct":%s,"disk_used_pct":%s,"jobs_running":%s,"bets_total_closed":"%s","fv_streams":%s,"actions":[%s]}\n' \
  "$TS" "$(state_of feature-computer)" "$(state_of smoke-strategy)" "$(state_of quant-redis)" \
  "$(state_of quant-timescaledb-1)" "$(state_of quant-backfill)" "${MEM_FREE_PCT:-0}" "${DISK_PCT:-0}" \
  "${JOBS_RUNNING:-0}" "${BETS:-na}" "${FV_STREAMS:-0}" "$ACTIONS" >> "$LOG"
