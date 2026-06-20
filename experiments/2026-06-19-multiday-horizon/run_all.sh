#!/usr/bin/env bash
# Autonomous detached run, MEMORY-BOUNDED: the daily-cache build is chunked across FRESH python processes
# (each caps RSS by exiting after a chunk of days — polars/Arrow allocations from the per-day scans
# accumulate ~17MB/day in a long-lived process, reclaimed at process exit). The resumable partition cache
# makes each chunk skip already-done days. Then ONE final process assembles the weekly panel + screens it,
# writing weekly_panel.parquet + screen_results.csv + .RUN_COMPLETE so the verdict survives a crash/session.
set -euo pipefail
cd /app
export PYTHONPATH=/app
DIR=experiments/2026-06-19-multiday-horizon
CHUNK="${CHUNK_DAYS:-300}"

echo "=== CACHE BUILD (chunked, $CHUNK days/process) $(date -u +%H:%M:%S) ==="
for _ in $(seq 1 100); do
  out=$(CACHE_ONLY=1 MAX_DAYS_PER_RUN="$CHUNK" python $DIR/build_weekly.py | tail -3)
  echo "$out"
  status=$(echo "$out" | grep CACHE_STATUS || true)
  cached=$(echo "$status" | sed -n 's/.*cached=\([0-9]*\).*/\1/p')
  total=$(echo "$status" | sed -n 's/.*total=\([0-9]*\).*/\1/p')
  if [ -n "$cached" ] && [ -n "$total" ] && [ "$cached" -ge "$total" ]; then
    echo "cache complete: $cached/$total"
    break
  fi
done

echo "=== ASSEMBLE + SCREEN $(date -u +%H:%M:%S) ==="
CACHE_ONLY=0 MAX_DAYS_PER_RUN=0 python $DIR/build_weekly.py
python $DIR/screen.py | tee $DIR/screen_console.txt

echo "=== DONE $(date -u +%H:%M:%S) ==="
touch $DIR/.RUN_COMPLETE
