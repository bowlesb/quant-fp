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
#   - trust_random_check : weekly RANDOM trust re-check (docs/TRUST_REDESIGN.md), Sat 14:45 PT.
#   - collect_jobs_status: refresh the /jobs dashboard's jobs_status.json every 5 min (off-:00), READ-ONLY.
#   - compact_stream     : nightly fold of SETTLED stream partitions' per-minute files, 22:33 PT weekdays.
#
#   ops/install_crons.sh --dry-run   # show what WOULD change; change nothing
#   ops/install_crons.sh             # install any missing managed cron
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
TRUST_LOG=/home/ben/.quant-validation/trust_random_check.log
JOBS_LOG=/home/ben/.quant-ops/collect_jobs_status.log
COMPACT_LOG=/home/ben/.quant-validation/compact_stream.log

# Each managed entry is a match-substring (presence test) + its crontab comment + its crontab line.
# Times are SYSTEM LOCAL TIME (America/Los_Angeles / PT); the crontab has no TZ override.
declare -a MATCHES COMMENTS LINES

# Sat 14:45 PT — off the weekday capture/sweep windows. Matches the registry row in docs/OPERATIONS.md.
MATCHES+=("ops/trust_random_check.sh")
COMMENTS+=("# Sat 14:45 PT weekly RANDOM trust re-check (docs/TRUST_REDESIGN.md) — conservative; only un-trusts a clean-day failure")
LINES+=("45 14 * * 6 cd $REPO && ops/trust_random_check.sh >> $TRUST_LOG 2>&1")

# Every 5 min on the :03/:08/... cadence (off-:00, staggered from the other crons). READ-ONLY: parses
# crontab -l + each cron's verify-log + docker ps and writes ~/.quant-ops/jobs_status.json for the /jobs page.
MATCHES+=("ops/collect_jobs_status.py")
COMMENTS+=("# every 5 min (off-:00) refresh the /jobs dashboard's jobs_status.json — READ-ONLY collector")
LINES+=("3-58/5 * * * * cd $REPO && python3 ops/collect_jobs_status.py >> $JOBS_LOG 2>&1")

# 22:33 PT weekdays — well after the 18:30 PT daily_lifecycle sweep (which READS stream partitions) and far
# off RTH; only folds days STRICTLY BEFORE today (fc writes only today's partition). Idempotent + atomic +
# reader-transparent — no fingerprint impact. See docs/STREAM_COMPACTION.md.
MATCHES+=("ops/compact_stream.sh")
COMMENTS+=("# 22:33 PT weekdays fold SETTLED stream partitions' per-minute files (docs/STREAM_COMPACTION.md) — idempotent, reader-transparent, settled-days-only")
LINES+=("33 22 * * 1-5 cd $REPO && ops/compact_stream.sh >> $COMPACT_LOG 2>&1")

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p /home/ben/.quant-validation /home/ben/.quant-ops

CHANGED=0
CURRENT="$(crontab -l 2>/dev/null || true)"
NEXT="$CURRENT"

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
