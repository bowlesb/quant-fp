#!/usr/bin/env bash
# Autonomous detached LP run: chunked-subprocess fill simulation (fresh process per chunk caps RSS —
# tick quote+trade replay is heavy) writing the per-(sym,day) fill ledger to the HOST-mounted fills/ dir
# (resumable: re-run skips done sym-days), then the median-anchored screen. Writes .RUN_COMPLETE so the
# verdict survives a crash/session.
set -euo pipefail
cd /app
export PYTHONPATH=/app
DIR=experiments/2026-06-20-liquidity-provision
CHUNK="${CHUNK_SYMS:-20}"

echo "=== FILL SIM (chunked, $CHUNK syms/process) $(date -u +%H:%M:%S) ==="
for _ in $(seq 1 500); do
  out=$(MAX_SYM_PER_RUN="$CHUNK" python $DIR/build_fills.py)
  echo "$out" | tail -2
  # the build prints "pending (sym,day)=N" at chunk start; N=0 → everything simmed, done.
  echo "$out" | grep -q "pending (sym,day)=0" && { echo "all sym-days simmed"; break; }
done

echo "=== SCREEN $(date -u +%H:%M:%S) ==="
python $DIR/screen.py | tee $DIR/screen_console.txt

echo "=== DONE $(date -u +%H:%M:%S) ==="
touch $DIR/.RUN_COMPLETE
