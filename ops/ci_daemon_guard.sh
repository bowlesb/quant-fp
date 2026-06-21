#!/usr/bin/env bash
# Respawn guard for the CI/CD daemons (docs/CONTINUOUS_DEPLOY.md, docs/CD_ARM_CHECKLIST.md).
#
# This box has no systemd-user; long-lived daemons are kept alive by a CHEAP cron that runs this guard every
# few minutes. The guard is idempotent: for each requested role it checks whether the supervisor
# (ops/ci_watcher.sh ROLE) is already running and, if not, (re)launches it detached via setsid + nohup so it
# survives this guard process exiting. A reboot loses the daemon; the next cron tick relaunches it.
#
#   ops/ci_daemon_guard.sh ci                # ensure the CI grade watcher supervisor is alive
#   ops/ci_daemon_guard.sh deploy            # ensure the auto-deploy watcher supervisor is alive
#   ops/ci_daemon_guard.sh ci deploy         # ensure both
#   ops/ci_daemon_guard.sh --status          # report liveness of every role, change nothing
#   ops/ci_daemon_guard.sh --stop ci         # stop the supervisor + its child python for a role
#
# PHASE GATING (the safety boundary — see docs/CD_ARM_CHECKLIST.md):
#   * The `ci` supervisor runs ops/ci_watcher.sh ci, which by DEFAULT here grades only (CI_NO_AUTO_MERGE=1 →
#     the watcher gets --no-auto-merge). To ARM Phase-2 auto-merge, set CI_NO_AUTO_MERGE=0 (Ben's click).
#   * The `deploy` supervisor runs ops/ci_watcher.sh deploy. To keep Phase-3 a DRY-RUN until armed, set
#     CI_DEPLOY_DRY_RUN=1 (the guard passes --dry-run through the env to the supervisor wrapper).
#
# SAFETY: this guard NEVER touches fc / strategies / docker; it only (re)launches the python watcher
# supervisors, which themselves carry every hard boundary (env-scrubbed CI containers, fail-closed scope,
# no fc restart, no fp-dev kill). The guard's only state change is spawning a supervisor if absent.
set -uo pipefail

REPO_DIR="${CI_REPO_DIR:-/home/ben/quant-fp}"
LOG_DIR="${HOME}/.quant-ops"
mkdir -p "$LOG_DIR"

# Phase gates (default = SAFEST: grade-only + deploy dry-run). Arming = flip these in the cron env.
NO_AUTO_MERGE="${CI_NO_AUTO_MERGE:-1}"   # 1 => watcher runs --no-auto-merge (Phase-1 grade-only)
DEPLOY_DRY_RUN="${CI_DEPLOY_DRY_RUN:-1}" # 1 => deploy watcher runs --dry-run (no container restart)

guard_log() { printf '%s ci-daemon-guard %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_DIR/ci_daemon_guard.log" >&2; }

# True if a supervisor for ROLE is currently running (matches the exact supervisor command line).
supervisor_pid() {
  local role="$1"
  pgrep -f "ci_watcher\.sh ${role}\b" | head -1
}

ensure_role() {
  local role="$1"
  local log="$LOG_DIR/ci_watcher.log"
  [ "$role" = "deploy" ] && log="$LOG_DIR/ci_deploy.log"

  local pid
  pid="$(supervisor_pid "$role")"
  if [ -n "$pid" ]; then
    guard_log "role=$role already supervised (pid $pid) — no action"
    return 0
  fi

  # Build the supervisor invocation with the phase-gated flags threaded through the wrapper's env.
  local -a env_prefix=()
  if [ "$role" = "ci" ] && [ "$NO_AUTO_MERGE" = "1" ]; then
    env_prefix+=("CI_WATCHER_ARGS=--no-auto-merge")
  fi
  if [ "$role" = "deploy" ] && [ "$DEPLOY_DRY_RUN" = "1" ]; then
    env_prefix+=("CI_DEPLOY_ARGS=--dry-run")
  fi

  guard_log "role=$role NOT running — launching supervisor (env: ${env_prefix[*]:-none})"
  ( cd "$REPO_DIR" && setsid env "${env_prefix[@]}" nohup "$REPO_DIR/ops/ci_watcher.sh" "$role" \
      >> "$log" 2>&1 < /dev/null & )
  sleep 1
  pid="$(supervisor_pid "$role")"
  if [ -n "$pid" ]; then
    guard_log "role=$role launched (pid $pid) -> $log"
  else
    guard_log "role=$role FAILED to launch (see $log)"
    return 1
  fi
}

stop_role() {
  local role="$1"
  local pid
  pid="$(supervisor_pid "$role")"
  if [ -z "$pid" ]; then
    guard_log "role=$role not running — nothing to stop"
    return 0
  fi
  # Kill the supervisor's whole process group so the child python watcher dies with it.
  guard_log "stopping role=$role (supervisor pid $pid + its group)"
  kill -- "-$(ps -o pgid= -p "$pid" | tr -d ' ')" 2>/dev/null || kill "$pid" 2>/dev/null
}

status() {
  for role in ci deploy; do
    local pid
    pid="$(supervisor_pid "$role")"
    if [ -n "$pid" ]; then
      printf 'role=%-7s SUPERVISED pid=%s\n' "$role" "$pid"
    else
      printf 'role=%-7s DOWN\n' "$role"
    fi
  done
}

main() {
  if [ "${1:-}" = "--status" ]; then
    status
    return 0
  fi
  if [ "${1:-}" = "--stop" ]; then
    shift
    for role in "$@"; do stop_role "$role"; done
    return 0
  fi
  if [ "$#" -eq 0 ]; then
    echo "usage: $0 {ci|deploy ...} | --status | --stop {ci|deploy ...}" >&2
    return 2
  fi
  for role in "$@"; do
    case "$role" in
      ci|deploy) ensure_role "$role" ;;
      *) echo "unknown role '$role' (expected ci|deploy)" >&2; return 2 ;;
    esac
  done
}

main "$@"
