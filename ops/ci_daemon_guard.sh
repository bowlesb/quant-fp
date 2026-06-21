#!/usr/bin/env bash
# Respawn guard for the CI/CD daemons (docs/CONTINUOUS_DEPLOY.md, docs/CD_ARM_CHECKLIST.md).
#
# This box has no systemd-user; long-lived daemons are kept alive by a CHEAP cron that runs this guard every
# few minutes. The guard is idempotent: for each requested role it checks whether the supervisor
# (ops/ci_watcher.sh ROLE) is already running and, if not, (re)launches it detached via setsid + nohup so it
# survives this guard process exiting. A reboot loses the daemon; the next cron tick relaunches it.
#
#   ops/ci_daemon_guard.sh grade             # ensure the Phase-1 GRADE-ONLY watcher is alive (never merges)
#   ops/ci_daemon_guard.sh ci                # ensure the auto-merge watcher is alive (Phase-2, armed)
#   ops/ci_daemon_guard.sh deploy            # ensure the auto-deploy watcher is alive (Phase-3)
#   ops/ci_daemon_guard.sh grade deploy      # ensure several roles
#   ops/ci_daemon_guard.sh --status          # report liveness of every role, change nothing
#   ops/ci_daemon_guard.sh --stop grade      # stop the supervisor + its child python for a role
#
# PHASE GATING (the safety boundary — see docs/CD_ARM_CHECKLIST.md):
#   * `grade` runs ops/ci_watcher.sh grade → the watcher gets --no-auto-merge: it ONLY posts a status +
#     sticky comment + tier label. It NEVER merges or deploys. This is the SAFE Phase-1 role the cron installs.
#   * `ci` runs ops/ci_watcher.sh ci → auto-merges green TIER-1 PRs (Phase-2). Arming = swap the cron from
#     `grade` to `ci` (Ben's click).
#   * `deploy` runs ops/ci_watcher.sh deploy → the Phase-3 auto-deploy watcher. Keep it DRY-RUN until armed by
#     setting CI_DEPLOY_DRY_RUN=1 (the guard passes --dry-run through to the deploy watcher).
#
# SAFETY: this guard NEVER touches fc / strategies / docker; it only (re)launches the python watcher
# supervisors, which themselves carry every hard boundary (env-scrubbed CI containers, fail-closed scope,
# no fc restart, no fp-dev kill). The guard's only state change is spawning a supervisor if absent.
set -uo pipefail

REPO_DIR="${CI_REPO_DIR:-/home/ben/quant-fp}"
LOG_DIR="${HOME}/.quant-ops"
mkdir -p "$LOG_DIR"

# Phase-3 deploy dry-run gate (default = SAFEST: dry-run, restart nothing). Arming = set CI_DEPLOY_DRY_RUN=0.
DEPLOY_DRY_RUN="${CI_DEPLOY_DRY_RUN:-1}"

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

  # The deploy role threads its dry-run flag through the wrapper env (CI_DEPLOY_ARGS); grade/ci need no env
  # (their --no-auto-merge / auto-merge mode is baked into the wrapper's role case).
  local -a env_prefix=()
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
  for role in grade ci deploy; do
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
    echo "usage: $0 {grade|ci|deploy ...} | --status | --stop {grade|ci|deploy ...}" >&2
    return 2
  fi
  for role in "$@"; do
    case "$role" in
      grade|ci|deploy) ensure_role "$role" ;;
      *) echo "unknown role '$role' (expected grade|ci|deploy)" >&2; return 2 ;;
    esac
  done
}

main "$@"
