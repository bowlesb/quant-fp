#!/usr/bin/env bash
# Supervised CI/CD daemon — runs the watchers in a restart loop (docs/CONTINUOUS_DEPLOY.md).
#
#   ops/ci_watcher.sh grade    # Phase-1 GRADE-ONLY: posts status/comment/label, NEVER auto-merges (safe rollout)
#   ops/ci_watcher.sh ci       # the CI gate + auto-merge watcher (Phase 1-2; auto-merges green TIER-1)
#   ops/ci_watcher.sh deploy   # the auto-deploy watcher (Phase 3)
#
# Each loop iteration runs the python daemon; if it dies it is restarted after a short backoff (so a
# transient gh/docker hiccup can't take CI offline). Logs to ~/.quant-ops/.
#
# SAFETY: the python watchers carry every hard boundary (no fc restart, no fp-dev kill, env-scrubbed CI
# containers, fail-closed scope). This wrapper only supervises them.
set -uo pipefail

ROLE="${1:-ci}"
REPO_DIR="${CI_REPO_DIR:-/home/ben/quant-fp}"
LOG_DIR="${HOME}/.quant-ops"
mkdir -p "$LOG_DIR"

case "$ROLE" in
  grade)
    # Phase-1 safe rollout: grade + status/comment/label only, NEVER auto-merge. Use this first; watch it
    # grade a few PRs correctly, THEN switch to the `ci` role to enable auto-merge.
    LOG="$LOG_DIR/ci_watcher.log"
    CMD=(python -m ops.ci_watcher --poll "${CI_POLL:-60}" --no-auto-merge)
    ;;
  ci)
    LOG="$LOG_DIR/ci_watcher.log"
    CMD=(python -m ops.ci_watcher --poll "${CI_POLL:-60}")
    ;;
  deploy)
    # CI_DEPLOY_ARGS lets the ci_daemon_guard keep Phase-3 a dry-run (--dry-run) until armed, without editing
    # this wrapper. Whitespace-split (the flag carries no spaces of its own); empty in the armed case.
    read -r -a DEPLOY_EXTRA <<< "${CI_DEPLOY_ARGS:-}"
    LOG="$LOG_DIR/ci_deploy.log"
    CMD=(python -m ops.ci_deploy --poll "${CI_POLL:-60}" "${DEPLOY_EXTRA[@]}")
    ;;
  *)
    echo "usage: $0 {grade|ci|deploy}" >&2
    exit 2
    ;;
esac

echo "$(date -u +%FT%TZ) ci_watcher.sh supervising role=$ROLE in $REPO_DIR -> $LOG" | tee -a "$LOG"
cd "$REPO_DIR" || exit 1

while true; do
  echo "$(date -u +%FT%TZ) starting: ${CMD[*]}" >> "$LOG"
  "${CMD[@]}" >> "$LOG" 2>&1
  code=$?
  echo "$(date -u +%FT%TZ) watcher exited code=$code; restarting in 10s" >> "$LOG"
  sleep 10
done
