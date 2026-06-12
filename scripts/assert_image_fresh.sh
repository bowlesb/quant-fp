#!/usr/bin/env bash
# RUNNING==INTENDED guard (task #11). For each compose service, assert its Docker image is at
# least as new as the last git commit touching the source baked into it (the service's own
# dir + shared quantlib for services that COPY it). Fail-loud (exit 1) on any stale/missing
# image so `docker compose run/up` never silently executes outdated code.
#
# WHY: `docker compose run/up` bakes source into the image (no volume mount); editing code then
# running WITHOUT rebuilding runs the OLD code. This has bitten the project 3x (universe ETF
# re-contamination 2026-06-12; stale experimenter VOL_FLOOR; stale-code experiment incident).
#
# Usage:
#   scripts/assert_image_fresh.sh                  # audit all services
#   scripts/assert_image_fresh.sh backfiller       # check one (use before a pipeline run)
# NOTE: compares against committed history; an image built from an UNCOMMITTED working tree
# reads as fresh. Commit before build to keep this honest.
set -euo pipefail
cd "$(dirname "$0")/.."

# Services that COPY shared quantlib into their image (so a quantlib commit makes them stale).
USES_QUANTLIB=" ingestor feature-computer model-server executor scheduler backfiller backfill-manager experimenter trainer "

ALL_SERVICES=(ingestor feature-computer model-server executor scheduler backfiller backfill-manager experimenter trainer dashboard)
services=("$@"); [ ${#services[@]} -eq 0 ] && services=("${ALL_SERVICES[@]}")

fail=0
for svc in "${services[@]}"; do
  img="quant-${svc}"
  created=$(docker image inspect "$img" --format '{{.Created}}' 2>/dev/null || true)
  if [ -z "$created" ]; then
    printf 'MISSING  %-22s (image never built)\n' "$img"; fail=1; continue
  fi
  created_epoch=$(date -d "$created" +%s)
  paths=("services/$svc")
  case "$USES_QUANTLIB" in *" $svc "*) paths+=("quantlib");; esac
  commit_epoch=$(git log -1 --format=%ct -- "${paths[@]}" 2>/dev/null || echo 0)
  if [ "${commit_epoch:-0}" -gt "$created_epoch" ]; then
    printf 'STALE    %-22s built %s  <  source commit %s  -> REBUILD\n' \
      "$img" "$(date -d "@$created_epoch" -Iseconds)" "$(date -d "@$commit_epoch" -Iseconds)"
    fail=1
  else
    printf 'fresh    %-22s\n' "$img"
  fi
done
[ "$fail" -eq 0 ] && echo "OK: all checked images are >= their source commits" || echo "FAIL: stale/missing images above — rebuild them (make rebuild S=<svc>)"
exit $fail
