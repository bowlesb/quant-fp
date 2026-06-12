#!/usr/bin/env bash
# BLOCKING freshness gate for tools-profile runs (task #11). NEVER run a one-shot tool
# (backfiller / trainer / …) on a stale image — image SHA must contain current source. This is the
# 4th-near-miss fix: a 14h-stale trainer image (predating the MODEL_FILENAME override) would have
# written the 21-feat model over the LIVE model_fwd_30m.txt. Manual diff-vigilance caught it 4×; the
# 5th time someone is tired. So this gate is structural: if the image is stale it is REBUILT to the
# current source SHA before the run (tired-proof — guarantees fresh code AND completes the task; a
# hard-fail would rely on a human rebuilding, which is the exact failure mode we're removing). The
# rebuild is loud. Caller env passes through, so existing `-e VAR=...` / exported-env runbook usage
# works unchanged. Usage: scripts/run_tool.sh <service> <args...>
set -euo pipefail
cd "$(dirname "$0")/.."

svc="${1:?usage: run_tool.sh <service> <args...>}"; shift || true

git_sha() {
  printf '%s' "$(git rev-parse --short HEAD)"
  git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || printf -- '-dirty'
}

# The GIT_SHA baked into an image (empty if none) — for explicit old->new provenance logging.
baked_sha() {
  docker inspect "quant-$1" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n 's/^GIT_SHA=//p' | head -n1
}

# Distinguish CONTENT-STALE (image missing a source commit = the dangerous near-miss class: BLOCK,
# rebuild) from DIRTY (image built from uncommitted WIP = ahead of commits, not behind: WARN, allow —
# else any peer's unrelated WIP would block every tool run in this shared worktree).
out="$(scripts/assert_image_fresh.sh "$svc" 2>&1 || true)"
if printf '%s\n' "$out" | grep -qE '^(STALE|MISSING|UNKNOWN) '; then
  echo "run_tool: $svc image is CONTENT-STALE vs source — REBUILDING before run (task #11 gate)" >&2
  printf '%s\n' "$out" | grep -E '^(STALE|MISSING|UNKNOWN) ' >&2
  old_sha="$(baked_sha "$svc")"; new_sha="$(git_sha)"
  echo "run_tool: REBUILD $svc  baked ${old_sha:-none} -> source ${new_sha}" >&2
  docker compose build --build-arg GIT_SHA="${new_sha}" "$svc"
  echo "run_tool: $svc rebuilt, now baked $(baked_sha "$svc") (was ${old_sha:-none})" >&2
  recheck="$(scripts/assert_image_fresh.sh "$svc" 2>&1 || true)"
  if printf '%s\n' "$recheck" | grep -qE '^(STALE|MISSING|UNKNOWN) '; then
    echo "run_tool: $svc STILL content-stale after rebuild — REFUSING to run (investigate)" >&2
    exit 1
  fi
elif printf '%s\n' "$out" | grep -qE '^(DIRTY|fresh\?) '; then
  echo "run_tool: WARN $svc image built from uncommitted WIP (-dirty) — running anyway (ahead of commits, not behind). Commit for clean provenance." >&2
else
  echo "run_tool: $svc image fresh (== source) — running" >&2
fi

exec docker compose --profile tools run --rm "$svc" "$@"
