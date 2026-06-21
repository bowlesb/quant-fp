#!/usr/bin/env bash
# Supervised CI/CD daemon — runs the watchers in a restart loop (docs/CONTINUOUS_DEPLOY.md).
#
#   ops/ci_watcher.sh ci       # the CI gate + auto-merge watcher (Phase 1-2)
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

# Phase-gating flags come in via the env (ci_daemon_guard sets them per the arm phase). They are APPENDED to
# the watcher command so the guard controls grade-only (--no-auto-merge) / deploy dry-run (--dry-run) without
# editing this wrapper. Whitespace-split (the flags carry no spaces of their own).
read -r -a CI_EXTRA <<< "${CI_WATCHER_ARGS:-}"
read -r -a DEPLOY_EXTRA <<< "${CI_DEPLOY_ARGS:-}"

case "$ROLE" in
  ci)
    LOG="$LOG_DIR/ci_watcher.log"
    CMD=(python -m ops.ci_watcher --poll "${CI_POLL:-60}" "${CI_EXTRA[@]}")
    ;;
  deploy)
    LOG="$LOG_DIR/ci_deploy.log"
    CMD=(python -m ops.ci_deploy --poll "${CI_POLL:-60}" "${DEPLOY_EXTRA[@]}")
    ;;
  *)
    echo "usage: $0 {ci|deploy}" >&2
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
