#!/usr/bin/env bash
# Independent auto-heal monitor for the LIVE trading apparatus. Runs from cron every few minutes,
# survives regardless of any interactive/agent session. It (a) restarts critical containers that have
# EXITED (the OOM/crash case), (b) guards host memory by pausing the backfill before it can starve the
# live capture, and (c) appends a JSON status line for the audit trail. It is deliberately conservative:
# it only restarts containers that are NOT running (never restart-loops a healthy-but-warming capture),
# leaving judgement calls (stalls, latency, bet-logic bugs) to the active oversight loop.
set -uo pipefail

LOG_DIR=/home/ben/.quant-ops
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/live_monitor.jsonl"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ACTIONS=""

note() { ACTIONS="${ACTIONS:+$ACTIONS,}\"$1\""; }

state_of() { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo missing; }

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

# --- 2. host-memory guard: if free RAM < 8%, pause the backfill so it can't OOM the live capture ---
MEM_FREE_PCT=$(free | awk '/Mem:/{printf "%d", ($7/$2)*100}')
if [ "${MEM_FREE_PCT:-100}" -lt 8 ]; then
  if [ "$(state_of quant-backfill)" = "running" ]; then
    docker stop quant-backfill >/dev/null 2>&1 && note "paused-backfill:lowmem(${MEM_FREE_PCT}%free)"
  fi
fi

# --- 3. disk guard: if the store partition is > 92% full, pause the backfill (writes the most data) ---
DISK_PCT=$(df --output=pcent /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9')
if [ "${DISK_PCT:-0}" -gt 92 ]; then
  if [ "$(state_of quant-backfill)" = "running" ]; then
    docker stop quant-backfill >/dev/null 2>&1 && note "paused-backfill:lowdisk(${DISK_PCT}%used)"
  fi
fi

# --- 4. quick observability snapshot for the audit trail ---
BETS=$(docker exec quant-timescaledb-1 psql -U quant -d quant -tA -c \
  "select count(*)||'/'||count(*) filter (where status='closed') from strat_smoke.bets" 2>/dev/null | tail -1)
FV_STREAMS=$(docker exec quant-redis redis-cli --scan --pattern 'fv:*' 2>/dev/null | wc -l | tr -d ' ')

printf '{"ts":"%s","containers":{"fc":"%s","smoke":"%s","redis":"%s","db":"%s","backfill":"%s"},"mem_free_pct":%s,"disk_used_pct":%s,"bets_total_closed":"%s","fv_streams":%s,"actions":[%s]}\n' \
  "$TS" "$(state_of feature-computer)" "$(state_of smoke-strategy)" "$(state_of quant-redis)" \
  "$(state_of quant-timescaledb-1)" "$(state_of quant-backfill)" "${MEM_FREE_PCT:-0}" "${DISK_PCT:-0}" \
  "${BETS:-na}" "${FV_STREAMS:-0}" "$ACTIONS" >> "$LOG"
