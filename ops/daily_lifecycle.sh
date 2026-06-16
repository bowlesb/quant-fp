#!/usr/bin/env bash
# DAILY self-sustaining parity-lifecycle chain: ACQUIRE the just-completed day's raw tape, then run the
# nightly parity-validation SWEEP (materialize backfill from that tape + validate live-vs-backfill + write
# the trust lifecycle). This is the missing day-2 link: the 6-month backfill (ops/raw_backfill.sh full) is
# a ONE-TIME job, so without this the nightly sweep would have NO fresh raw bars for the just-closed day and
# could never materialize its backfill side. This wrapper chains the existing, unchanged engines — it does
# NOT reimplement any logic:
#
#   1. ACQUIRE  -> quantlib.data.raw_backfill --days N   (full universe + SPY/QQQ, last N settled days -> /store/raw)
#   2. SWEEP    -> ops/validation_sweep.sh               (materialize_from_raw -> validate -> trust lifecycle)
#
# The acquire is idempotent (manifest skips already-fetched symbol-days), so a re-run only fetches what is
# missing. The sweep targets the LAST MARKET DAY by default (settled T+1), matching the raw days acquired.
# Market-reference tickers (SPY/QQQ) are screened out of the universe but REQUIRED in /store/raw for the
# cross-sectional features (market_beta/idio_vol/market_return/...) to validate — raw_backfill now appends
# them to the fetched universe unconditionally, and the sweep pins them into every materialize chunk.
#
# Install (LEAD): run AFTER the nightly_relaunch + AFTER market close. Suggested cron (18:30 PT = before the
# existing 19:30 PT validation_sweep, so the raw tape lands first):
#
#   30 18 * * 1-5 cd /home/ben/quant-fp && ops/daily_lifecycle.sh >> /home/ben/.quant-validation/daily_lifecycle.log 2>&1
#
# (If you install this, REMOVE the standalone 19:30 validation_sweep cron line — this chain runs the sweep
# itself. Keeping both is harmless but redundant: the sweep is idempotent.)
#
# Usage:
#   ops/daily_lifecycle.sh                  # acquire last 2 settled days (D, D-1), then sweep the last market day
#   DAYS=3 ops/daily_lifecycle.sh           # widen the acquire window (catch up several missed nights)
#   DAY=2026-06-12 ops/daily_lifecycle.sh   # sweep a specific settled day (still acquires the recent window)
#   SKIP_ACQUIRE=1 ops/daily_lifecycle.sh   # sweep only (raw tape already present)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The sweep validates the day STRICTLY BEFORE today (the last SETTLED session — validation_sweep
# .last_market_day). Running after close on session D, that target is D-1, while a 1-day acquire fetches
# D (today). So acquire the last 2 trading days (D and D-1) to GUARANTEE the sweep's target day's raw tape
# is present. The acquire is idempotent (manifest-skips), so the extra day is ~free on a re-run.
DAYS="${DAYS:-2}"                # settled trading days of raw tape to (re)acquire each night
TOP_TRADES="${TOP_TRADES:-1500}"
TOP_QUOTES="${TOP_QUOTES:-300}"
BUDGET_TB="${BUDGET_TB:-1.8}"

log() { printf '[daily_lifecycle] %s\n' "$*" >&2; }

if [ -z "${SKIP_ACQUIRE:-}" ]; then
  log "STAGE 1/2 ACQUIRE: full universe + SPY/QQQ, last ${DAYS} settled trading day(s) -> /store/raw"
  DAYS="$DAYS" TOP_TRADES="$TOP_TRADES" TOP_QUOTES="$TOP_QUOTES" BUDGET_TB="$BUDGET_TB" \
    "$HERE/raw_backfill.sh" daily || { log "ACQUIRE failed (exit $?) — aborting before sweep"; exit 1; }
else
  log "SKIP_ACQUIRE set — skipping stage 1; sweeping existing /store/raw tape"
fi

log "STAGE 2/2 SWEEP: materialize backfill + validate live-vs-backfill + write trust lifecycle"
exec "$HERE/validation_sweep.sh"
