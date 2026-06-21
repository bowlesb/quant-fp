#!/usr/bin/env bash
# Idempotent installer for the registry crons (docs/OPERATIONS.md) that the hand-managed crontab is missing.
# The crontab has no single source that materializes the registry, so documented entries can drift out of
# the live crontab. This script closes those specific gaps WITHOUT touching any existing cron line: for each
# managed entry below it appends the line only if its match-substring is absent, and is a no-op if present.
#
# Deliberately narrow: it never rewrites or de-duplicates the other (hand-managed) entries, so it can never
# double-install the DESTRUCTIVE nightly_relaunch fc-recreate. Broadening this into a full registry
# reconciler requires first de-duplicating the existing hand-added lines — a coordinator-owned migration,
# not something this script does unilaterally.
#
# Managed entries:
#   - trust_random_check : DAILY RANDOM trust re-check (docs/TRUST_REDESIGN.md), 14:45 PT (skips unsettled days).
#   - compact_stream     : nightly fold of SETTLED stream partitions' per-minute files, 22:33 PT weekdays.
#   - late re-sweep      : 23:30 PT weekday re-acquire + re-sweep once Alpaca's illiquid tail has settled
#                          (the 18:30 sweep often RawNotSettled-SKIPs) — the trust-jump unblock.
#
#   ops/install_crons.sh --dry-run   # show what WOULD change; change nothing
#   ops/install_crons.sh             # install any missing managed cron
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
TRUST_LOG=/home/ben/.quant-validation/trust_random_check.log
COMPACT_LOG=/home/ben/.quant-validation/compact_stream.log
LATE_SWEEP_LOG=/home/ben/.quant-validation/daily_lifecycle_late.log
GLIMPSE_LOG=/home/ben/.quant-ops/collect_store_glimpse.log

# Each managed entry is a match-substring (presence test) + its crontab comment + its crontab line.
# Times are SYSTEM LOCAL TIME (America/Los_Angeles / PT); the crontab has no TZ override.
declare -a MATCHES COMMENTS LINES

# 14:45 PT DAILY — post-close, off the capture/sweep windows. Matches the registry row in docs/OPERATIONS.md.
# Daily (not weekly) so trust regressions are caught fast during active development; the script skips unsettled days.
MATCHES+=("ops/trust_random_check.sh")
COMMENTS+=("# 14:45 PT DAILY RANDOM trust re-check (docs/TRUST_REDESIGN.md) — conservative; only un-trusts a clean-day failure; skips unsettled days")
LINES+=("45 14 * * * cd $REPO && ops/trust_random_check.sh >> $TRUST_LOG 2>&1")

# Every 3 min on the :01/:04/... cadence (off-:00, staggered from the other collectors). PRECOMPUTE the
# /store-glimpse grid + per-group ticker drills into the persistent (Redis) cache so the page serves sub-ms
# instead of paying the ~50s build on each refresh. The build needs quantlib/polars + the /store mount, so
# this wrapper execs `python -m store_glimpse_cache` INSIDE quant-dashboard-1 (same docker-exec pattern
# healthcheck uses) — read-only w.r.t. the store, idempotent (just overwrites the Redis blobs). ~105s/run < 3m.
MATCHES+=("ops/collect_store_glimpse.py")
COMMENTS+=("# every 3 min (off-:00) precompute the /store-glimpse grid + drills into the Redis cache — execs python -m store_glimpse_cache in quant-dashboard-1; READ-ONLY store, idempotent")
LINES+=("1-58/3 * * * * cd $REPO && python3 ops/collect_store_glimpse.py >> $GLIMPSE_LOG 2>&1")

# 22:33 PT weekdays — well after the 18:30 PT daily_lifecycle sweep (which READS stream partitions) and far
# off RTH; only folds days STRICTLY BEFORE today (fc writes only today's partition). Idempotent + atomic +
# reader-transparent — no fingerprint impact. See docs/STREAM_COMPACTION.md.
MATCHES+=("ops/compact_stream.sh")
COMMENTS+=("# 22:33 PT weekdays fold SETTLED stream partitions' per-minute files (docs/STREAM_COMPACTION.md) — idempotent, reader-transparent, settled-days-only")
LINES+=("33 22 * * 1-5 cd $REPO && ops/compact_stream.sh >> $COMPACT_LOG 2>&1")

# 23:30 PT weekdays — LATE re-acquire + re-sweep. Alpaca's illiquid-tail SIP historical bars often have not
# settled by the 18:30 PT sweep (~5h post-close) so it RawNotSettled-SKIPs (06-17=65%/06-18=56% < the 90%
# assert_tail_settled gate) → 0 newly-trusted. By 23:30 PT (~10.5h post-close) the tail has settled; this
# re-runs the SAME chained daily_lifecycle.sh (idempotent re-acquire re-fetches the 0-row manifest entries
# left at 18:30, then re-sweeps to grade). The direct trust-jump unblock. Off RTH, after the 22:33 compaction.
# MATCH on the unique LATE log path (NOT "ops/daily_lifecycle.sh", which the existing 18:30 line already
# carries — matching the script name would see it "present" and never install this second line).
MATCHES+=("daily_lifecycle_late.log")
COMMENTS+=("# 23:30 PT weekdays LATE re-acquire + re-sweep (docs/OPERATIONS.md) — Alpaca tail settles after the 18:30 sweep; idempotent, no fingerprint")
LINES+=("30 23 * * 1-5 cd $REPO && ops/daily_lifecycle.sh >> $LATE_SWEEP_LOG 2>&1")

# Every 5 min — KEEP the CI GRADE daemon alive (docs/CD_ARM_CHECKLIST.md Phase 1). The guard is a no-op when
# the supervisor is already running and (re)launches it (incl. after a reboot) otherwise. This is the SAFE
# Phase-1 stage: the `grade` role runs the watcher with --no-auto-merge, so it only posts a commit STATUS +
# sticky comment + tier label on each open PR's new head SHA — it NEVER merges or deploys. Arming auto-merge
# (Phase 2 = swap `grade`→`ci`) / auto-deploy (Phase 3) is a SEPARATE, Ben-gated step documented in
# docs/CD_ARM_CHECKLIST.md — deliberately NOT auto-installed.
MATCHES+=("ops/ci_daemon_guard.sh grade")
COMMENTS+=("# every 5 min keep the CI GRADE daemon alive (docs/CD_ARM_CHECKLIST.md Phase 1) — grade-only (--no-auto-merge); NEVER merges/deploys")
LINES+=("*/5 * * * * cd $REPO && ops/ci_daemon_guard.sh grade >> /home/ben/.quant-ops/ci_daemon_guard.log 2>&1")

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p /home/ben/.quant-validation /home/ben/.quant-ops

CHANGED=0
CURRENT="$(crontab -l 2>/dev/null || true)"
NEXT="$CURRENT"

# AUDIT (non-mutating): the hand-managed nightly_relaunch line must carry UNIVERSE_MAX_SYMBOLS, else each
# pre-market relaunch silently re-seeds the universe at seed_universe's DEFAULT cap (3000) instead of the
# full filtered set. This was a real oversight (06-16..06-20: membership re-capped 11336->3000 every night)
# because the override was never on the cron line. We do NOT rewrite the DESTRUCTIVE relaunch line (see the
# header) — instead we LOUDLY flag the gap so a relaunch can never silently re-cap unnoticed. The coordinator
# applies the one-line live-crontab edit (the canonical line is in docs/OPERATIONS.md).
audit_relaunch_universe_cap() {
  printf '%s\n' "$CURRENT" | grep -F 'ops/nightly_relaunch.sh' | grep -vqF 'UNIVERSE_MAX_SYMBOLS' || return 0
  echo "install_crons.sh: ⚠️  nightly_relaunch cron line is MISSING UNIVERSE_MAX_SYMBOLS — it will re-seed"
  echo "                  the universe at the DEFAULT 3000 cap. Coordinator: edit the live crontab line to"
  echo "                  prefix 'UNIVERSE_MAX_SYMBOLS=100000' (see docs/OPERATIONS.md registry). Not auto-fixed"
  echo "                  here: this installer never rewrites the destructive relaunch line."
}
audit_relaunch_universe_cap

for i in "${!MATCHES[@]}"; do
  match="${MATCHES[$i]}"
  if printf '%s\n' "$NEXT" | grep -qF "$match"; then
    echo "install_crons.sh: '$match' already present — no change."
    continue
  fi
  echo "install_crons.sh: '$match' MISSING — will append:"
  printf '  %s\n  %s\n' "${COMMENTS[$i]}" "${LINES[$i]}"
  NEXT="$(printf '%s\n%s\n%s\n' "$NEXT" "${COMMENTS[$i]}" "${LINES[$i]}")"
  CHANGED=1
done

if [ "$CHANGED" = 0 ]; then
  echo "install_crons.sh: all managed crons already present — nothing to do."
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "=== install_crons.sh --dry-run: the above entries WOULD be appended (no change made) ==="
  exit 0
fi

printf '%s\n' "$NEXT" | crontab -
echo "install_crons.sh: installed missing managed crons."
