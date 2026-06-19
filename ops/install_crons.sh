#!/usr/bin/env bash
# Idempotent installer for the ONE cron that the registry (docs/OPERATIONS.md) documents but that drifted
# out of the live crontab: the weekly RANDOM trust re-check (ops/trust_random_check.sh). The crontab is
# hand-managed with no single source that materializes the registry, so this entry was documented but never
# installed. This script closes that specific gap WITHOUT touching any existing cron line: it adds the
# trust_random_check entry only if it is absent, and is a no-op if it is already present.
#
# Deliberately narrow: it never rewrites or de-duplicates the other (hand-managed) entries, so it can never
# double-install the DESTRUCTIVE nightly_relaunch fc-recreate. Broadening this into a full registry
# reconciler requires first de-duplicating the existing hand-added lines — a coordinator-owned migration,
# not something this script does unilaterally.
#
#   ops/install_crons.sh --dry-run   # show what WOULD change; change nothing
#   ops/install_crons.sh             # install the trust_random_check cron if missing
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
LOG=/home/ben/.quant-validation/trust_random_check.log

# Sat 14:45 PT (system local time = America/Los_Angeles; the crontab has no TZ override). Off the weekday
# capture/sweep windows. Matches the registry row in docs/OPERATIONS.md.
CRON_LINE="45 14 * * 6 cd $REPO && ops/trust_random_check.sh >> $LOG 2>&1"
CRON_COMMENT="# Sat 14:45 PT weekly RANDOM trust re-check (docs/TRUST_REDESIGN.md) — conservative; only un-trusts a clean-day failure"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

CURRENT="$(crontab -l 2>/dev/null || true)"

if printf '%s\n' "$CURRENT" | grep -qF "ops/trust_random_check.sh"; then
  echo "install_crons.sh: trust_random_check cron already present — no change."
  exit 0
fi

NEW_CRONTAB="$(printf '%s\n%s\n%s\n' "$CURRENT" "$CRON_COMMENT" "$CRON_LINE")"

if [ "$DRY_RUN" = 1 ]; then
  echo "=== install_crons.sh --dry-run: would APPEND this entry ==="
  printf '%s\n%s\n' "$CRON_COMMENT" "$CRON_LINE"
  exit 0
fi

mkdir -p /home/ben/.quant-validation
printf '%s\n' "$NEW_CRONTAB" | crontab -
echo "install_crons.sh: installed trust_random_check cron (Sat 14:45 PT)."
