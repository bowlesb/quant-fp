#!/usr/bin/env bash
# RUNNING==INTENDED guard (task #11). For each compose service, assert that the code actually
# RUNNING (the container's image, or the built image if not running) contains the latest committed
# source for that service. Fail-loud (exit 1) on any stale/dirty/missing image so a code edit is
# never silently un-deployed.
#
# WHY: `docker compose run/up` bakes source into the image (no volume mount); editing code then
# running WITHOUT rebuilding runs the OLD code. This has bitten the project 4x (universe ETF
# re-contamination, stale experimenter VOL_FLOOR, stale-code experiment incident, the #13 verify
# scare). Clock-based freshness is fragile (an image can be built seconds BEFORE its own commit and
# still contain the committed code) — so v2 checks by CONTENT: a git SHA baked into each image at
# build time (ARG GIT_SHA, injected by `make rebuild`/`make rebuild-all`).
#
# Check, per service: the baked SHA must (a) exist in local history, (b) not be -dirty, and
# (c) contain the last commit that touched the service's source (its dir + shared quantlib). If an
# image carries no GIT_SHA (legacy, pre-#11), fall back to the old timestamp comparison and flag it.
#
# Usage:
#   scripts/assert_image_fresh.sh                  # audit all services
#   scripts/assert_image_fresh.sh scheduler        # check one (use before a pipeline run/restart)
set -euo pipefail
cd "$(dirname "$0")/.."

# Services that COPY shared quantlib into their image (so a quantlib commit makes them stale).
USES_QUANTLIB=" ingestor feature-computer model-server executor scheduler backfiller backfill-manager experimenter trainer "

ALL_SERVICES=(ingestor feature-computer model-server executor scheduler backfiller backfill-manager experimenter trainer dashboard)
services=("$@"); [ ${#services[@]} -eq 0 ] && services=("${ALL_SERVICES[@]}")

# Print the GIT_SHA env baked into a docker object (container or image); empty if absent.
baked_sha_of() {
  docker inspect "$1" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n 's/^GIT_SHA=//p' | head -n1
}

fail=0
for svc in "${services[@]}"; do
  img="quant-${svc}"
  container="quant-${svc}-1"
  paths=("services/$svc")
  case "$USES_QUANTLIB" in *" $svc "*) paths+=("quantlib");; esac

  # Prefer the RUNNING container (running==intended); else the built image; else missing.
  if docker inspect "$container" >/dev/null 2>&1; then
    ref="$container"; loc="running"
  elif docker image inspect "$img" >/dev/null 2>&1; then
    ref="$img"; loc="image"
  else
    printf 'MISSING  %-20s (image never built)\n' "$svc"; fail=1; continue
  fi

  src_last=$(git log -1 --format=%H -- "${paths[@]}" 2>/dev/null || echo "")
  baked=$(baked_sha_of "$ref")

  if [ -n "$baked" ] && [ "$baked" != "unknown" ]; then
    # Content-based check.
    if [ "${baked%-dirty}" != "$baked" ]; then
      printf 'DIRTY    %-20s baked=%s (built from uncommitted tree) -> commit+rebuild\n' "$svc" "$baked"
      fail=1; continue
    fi
    if ! git cat-file -e "${baked}^{commit}" 2>/dev/null; then
      printf 'UNKNOWN  %-20s baked=%s not in local history -> rebuild\n' "$svc" "$baked"
      fail=1; continue
    fi
    if [ -z "$src_last" ] || git merge-base --is-ancestor "$src_last" "$baked" 2>/dev/null; then
      printf 'fresh    %-20s (%s, baked %s)\n' "$svc" "$loc" "$baked"
    else
      printf 'STALE    %-20s baked %s does NOT contain source commit %s -> REBUILD\n' \
        "$svc" "$baked" "$(git rev-parse --short "$src_last")"
      fail=1
    fi
  else
    # Legacy fallback: no SHA baked in — use the build-time clock vs last source commit.
    created=$(docker inspect "$ref" --format '{{.Created}}' 2>/dev/null || true)
    created_epoch=$(date -d "$created" +%s 2>/dev/null || echo 0)
    commit_epoch=$(git log -1 --format=%ct -- "${paths[@]}" 2>/dev/null || echo 0)
    if [ "${commit_epoch:-0}" -gt "$created_epoch" ]; then
      printf 'STALE    %-20s (legacy, no SHA) built %s < commit %s -> REBUILD\n' \
        "$svc" "$(date -d "@$created_epoch" -Iseconds)" "$(date -d "@$commit_epoch" -Iseconds)"
      fail=1
    else
      printf 'fresh?   %-20s (legacy, no SHA stamp — rebuild to enable content check)\n' "$svc"
    fi
  fi
done
[ "$fail" -eq 0 ] && echo "OK: running code contains latest committed source for all checked services" \
  || echo "FAIL: stale/dirty/missing above — rebuild them (make rebuild S=<svc> | make rebuild-all)"
exit $fail
