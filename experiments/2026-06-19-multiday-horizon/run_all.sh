#!/usr/bin/env bash
# Autonomous detached run: build the full weekly panel (resumable daily cache), then screen it through every
# gate, writing weekly_panel.parquet + screen_results.csv so the verdict survives a session boundary / crash.
set -euo pipefail
cd /app
export PYTHONPATH=/app
DIR=experiments/2026-06-19-multiday-horizon

echo "=== BUILD $(date -u +%H:%M:%S) ==="
python $DIR/build_weekly.py

echo "=== SCREEN $(date -u +%H:%M:%S) ==="
python $DIR/screen.py | tee $DIR/screen_console.txt

echo "=== DONE $(date -u +%H:%M:%S) ==="
touch $DIR/.RUN_COMPLETE
