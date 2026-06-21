#!/usr/bin/env bash
# Host-side cron wrapper for the non-tape data-freshness alert (SEC EDGAR filings + Alpaca news).
#
# Closes the #311 monitoring blind spot: quant-edgar-1 logs "poll: 100 filings upserted" every cycle even
# when every row is an ON CONFLICT no-op, so a real ingest STALL looks identical to health. This probe
# reads the TRUE newest ingest instant for each source (max discovered_at for live stream filings; max
# live-arrival available_at in /store/news) and WARNs / STALEs when ingest has stalled beyond a
# market-hours-aware threshold (weekend/overnight SEC lulls are expected and never alert).
#
# The probe needs quantlib + polars + psycopg + the /store mount + DB creds — none of which the HOST python
# carries. So, exactly like ops/healthcheck.sh and ops/collect_store_glimpse.py, this wrapper execs the
# module INSIDE the quant-dashboard-1 container (which has all of those + a read-only /store + DB env + a
# route to timescaledb). It is READ-ONLY: a DB SELECT + a polars scan of the newest few news partitions.
# It NEVER touches the live ingesters (quant-edgar-1 / news-capture) — pure monitoring.
#
# Appends the JSON status line to the log for the audit trail and propagates the module's exit code
# (0 = no source STALE, 1 = at least one STALE during business hours).
#
#   ops/data_freshness.sh            # run once, log + propagate exit code (the cron form)
set -uo pipefail

CONTAINER="${FRESHNESS_CONTAINER:-quant-dashboard-1}"
MODULE="quantlib.ops.data_freshness"
LOG_DIR="${FRESHNESS_LOG_DIR:-/home/ben/.quant-ops}"
LOG="$LOG_DIR/data_freshness.jsonl"

mkdir -p "$LOG_DIR"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  printf '{"ts":"%s","error":"container %s missing"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CONTAINER" >> "$LOG"
  echo "[data_freshness.sh] FAIL: container '$CONTAINER' not found" >&2
  exit 2
fi

# Capture the JSON status line (stdout) for the log; let the module's WARN/INFO lines (stderr) flow to the
# cron log. The module exits 1 if any source is STALE during business hours.
OUT=$(docker exec "$CONTAINER" python -m "$MODULE" --json 2>&1)
CODE=$?

# The last line is the JSON status line; the rest are the human WARN/INFO lines.
echo "$OUT" >&2
printf '%s\n' "$OUT" | grep -E '^\{' | tail -1 >> "$LOG"

exit $CODE
