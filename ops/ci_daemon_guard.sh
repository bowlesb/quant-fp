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

# Two DISTINCT trees, decoupled on purpose:
#   * LIVE_TREE  — the fc bind-mount tree. It is PINNED at a controlled SHA (FF-ing it is the gated
#                  fc-relaunch DEPLOY step, never a CI convenience). The deploy role reads it (ci_deploy does
#                  the real `compose up` there). The grade/ci roles must NOT depend on it: it can lag main and
#                  may not even contain this script (it was added after the pin → the grade cron "not found"s).
#   * CI_TREE    — a DEDICATED checkout the guard keeps current with origin/main (fetch + reset --hard each
#                  cycle, self-healing). The grade/ci watcher runs from HERE, so its code + exclude policy are
#                  always current with main, fully decoupled from the pinned fc tree. NOTHING bind-mounts it.
LIVE_TREE="${CI_LIVE_TREE:-/home/ben/quant-fp}"
CI_TREE="${CI_TREE:-/home/ben/.ci-repo}"
LOG_DIR="${HOME}/.quant-ops"
mkdir -p "$LOG_DIR"

# Phase-3 deploy dry-run gate (default = SAFEST: dry-run, restart nothing). Arming = set CI_DEPLOY_DRY_RUN=0.
DEPLOY_DRY_RUN="${CI_DEPLOY_DRY_RUN:-1}"

guard_log() { printf '%s ci-daemon-guard %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_DIR/ci_daemon_guard.log" >&2; }

# Provision-or-refresh the dedicated CI checkout to origin/main and echo the tree to run the grader from.
# Self-healing: clones if absent (or if the path exists but isn't a git repo — wipe + re-clone), then
# `fetch origin main` + `reset --hard origin/main` (idempotent; a divergent/dirty CI tree is forcibly reset —
# it's a throwaway grading checkout, never hand-edited). On ANY failure it falls back to LIVE_TREE so a
# provisioning hiccup degrades to the old behavior rather than not grading at all. Echoes the chosen tree.
ensure_ci_repo() {
  local origin
  origin="$(git -C "$LIVE_TREE" remote get-url origin 2>/dev/null || echo https://github.com/bowlesb/quant-fp.git)"

  if [ ! -d "$CI_TREE/.git" ]; then
    [ -e "$CI_TREE" ] && { guard_log "CI_TREE $CI_TREE exists but is not a git repo — wiping for re-clone"; rm -rf "$CI_TREE"; }
    guard_log "provisioning dedicated CI checkout at $CI_TREE (clone $origin)"
    if ! git clone -q "$origin" "$CI_TREE" 2>>"$LOG_DIR/ci_daemon_guard.log"; then
      guard_log "CI checkout clone FAILED — falling back to LIVE_TREE $LIVE_TREE"
      printf '%s' "$LIVE_TREE"; return 1
    fi
  fi

  if git -C "$CI_TREE" fetch -q origin main 2>>"$LOG_DIR/ci_daemon_guard.log" \
     && git -C "$CI_TREE" reset -q --hard origin/main 2>>"$LOG_DIR/ci_daemon_guard.log"; then
    guard_log "CI checkout $CI_TREE reset to origin/main $(git -C "$CI_TREE" rev-parse --short HEAD)"
    printf '%s' "$CI_TREE"
  else
    guard_log "CI checkout refresh FAILED — falling back to LIVE_TREE $LIVE_TREE"
    printf '%s' "$LIVE_TREE"; return 1
  fi
}

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

  # grade/ci run from the dedicated, always-current CI checkout (so their code + exclude policy match main and
  # the script is guaranteed present). deploy runs from the fc-mounted LIVE_TREE, because ci_deploy performs
  # the real `compose up`/FF there (it reads CI_LIVE_TREE). CI_REPO_DIR is set so the watcher's git/gh/worktree
  # ops happen in the CI tree, never the pinned fc tree.
  local tree="$LIVE_TREE"
  local -a env_prefix=()
  if [ "$role" = "grade" ] || [ "$role" = "ci" ]; then
    tree="$(ensure_ci_repo)"
    env_prefix+=("CI_REPO_DIR=$tree")
  fi
  if [ "$role" = "deploy" ] && [ "$DEPLOY_DRY_RUN" = "1" ]; then
    env_prefix+=("CI_DEPLOY_ARGS=--dry-run")
  fi

  guard_log "role=$role NOT running — launching supervisor from $tree (env: ${env_prefix[*]:-none})"
  ( cd "$tree" && setsid env "${env_prefix[@]}" nohup "$tree/ops/ci_watcher.sh" "$role" \
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
