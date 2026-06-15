#!/usr/bin/env bash
# Dedicated data-quality AUDIT loop driver — one headless Claude cycle that audits feature-group data
# and applies local fixes until every group is OK (see docs/DATA_QUALITY_LEDGER.md).
# Runs in parallel with ops/autonomous_loop.sh (the main 1/2/3 loop) — SEPARATE lock, different lane.
set -uo pipefail

REPO=/home/ben/quant-fp
PROMPT_FILE="$REPO/ops/audit_loop_prompt.txt"
LOG_DIR=/home/ben/.quant-loop
LOCK="$LOG_DIR/audit.lock"   # OWN lock (not the main loop's) so the two can run concurrently
MODEL=claude-opus-4-8
MAX_SECONDS=1500

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/audit-cycle-$STAMP.log"
CLAUDE_BIN="$(command -v claude || echo /home/ben/.local/bin/claude)"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) previous audit cycle still running — skipping this tick" >>"$LOG_DIR/audit-skips.log"
  exit 0
fi

cd "$REPO" || exit 1
echo "$(date -Is) starting audit cycle (model=$MODEL)" >"$LOG"
timeout "$MAX_SECONDS" "$CLAUDE_BIN" --print \
  --dangerously-skip-permissions \
  --model "$MODEL" \
  <"$PROMPT_FILE" >>"$LOG" 2>&1
echo "$(date -Is) audit cycle exit=$? (ceiling ${MAX_SECONDS}s)" >>"$LOG"

ls -1t "$LOG_DIR"/audit-cycle-*.log 2>/dev/null | tail -n +201 | xargs -r rm -f
exit 0
