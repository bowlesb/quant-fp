#!/usr/bin/env bash
# Autonomous maintainer+builder loop driver — runs ONE headless Claude cycle.
# Invoked by host cron; fully independent of any interactive Claude session.
# A flock prevents overlapping cycles; a timeout bounds each run under the cron cadence.
set -uo pipefail

REPO=/home/ben/quant-fp
PROMPT_FILE="$REPO/ops/autonomous_loop_prompt.txt"
LOG_DIR=/home/ben/.quant-loop
LOCK="$LOG_DIR/loop.lock"
MODEL=claude-opus-4-8
MAX_SECONDS=1500   # 25 min ceiling so a cycle never overruns the 30-min cadence

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/cycle-$STAMP.log"
CLAUDE_BIN="$(command -v claude || echo /home/ben/.local/bin/claude)"

# Non-blocking lock: if a previous cycle is still running, skip this tick.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) previous cycle still running — skipping this tick" >>"$LOG_DIR/skips.log"
  exit 0
fi

cd "$REPO" || exit 1
echo "$(date -Is) starting autonomous cycle (model=$MODEL)" >"$LOG"
timeout "$MAX_SECONDS" "$CLAUDE_BIN" --print \
  --dangerously-skip-permissions \
  --model "$MODEL" \
  <"$PROMPT_FILE" >>"$LOG" 2>&1
STATUS=$?
echo "$(date -Is) cycle exit=$STATUS (timeout ceiling ${MAX_SECONDS}s)" >>"$LOG"

# Keep only the last 200 cycle logs.
ls -1t "$LOG_DIR"/cycle-*.log 2>/dev/null | tail -n +201 | xargs -r rm -f
exit 0
