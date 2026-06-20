#!/usr/bin/env bash
# Autonomous news-hotness run: chunked daily-cache build (shared across embargos), then the news panel for
# EACH embargo {1,5,15} (the load-bearing feed-delay stability sweep), then the screen. Host-mounted
# resumable cache + .RUN_COMPLETE so the verdict survives a crash/session.
set -euo pipefail
cd /app
export PYTHONPATH=/app
DIR=experiments/2026-06-20-news-hotness
CHUNK="${CHUNK_DAYS:-60}"

echo "=== DAILY CACHE (chunked, $CHUNK days/process) $(date -u +%H:%M:%S) ==="
for _ in $(seq 1 50); do
  out=$(CACHE_ONLY=1 MAX_DAYS_PER_RUN="$CHUNK" python $DIR/build_news_panel.py)
  echo "$out" | tail -2
  echo "$out" | grep -q "pending: 0\|cached=$(echo "$out"|grep -o 'total=[0-9]*'|cut -d= -f2)" && true
  # done when a chunk reports no pending work (cached==total)
  st=$(echo "$out" | grep -o 'CACHE_STATUS cached=[0-9]* total=[0-9]*')
  c=$(echo "$st" | sed -n 's/.*cached=\([0-9]*\).*/\1/p'); t=$(echo "$st" | sed -n 's/.*total=\([0-9]*\).*/\1/p')
  [ -n "$c" ] && [ -n "$t" ] && [ "$c" -ge "$t" ] && { echo "cache complete $c/$t"; break; }
done

for emb in 1 5 15; do
  echo "=== NEWS PANEL embargo=$emb $(date -u +%H:%M:%S) ==="
  EMBARGO_MIN=$emb CACHE_ONLY=0 python $DIR/build_news_panel.py
done

echo "=== SCREEN $(date -u +%H:%M:%S) ==="
python $DIR/screen.py | tee $DIR/screen_console.txt

echo "=== DONE $(date -u +%H:%M:%S) ==="
touch $DIR/.RUN_COMPLETE
