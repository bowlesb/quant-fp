#!/usr/bin/env bash
# Team brief: what CHANGED since the last team review + current state. The manager runs
# this at the START of every wake and includes its output in EVERY specialist agent's
# prompt, so no agent depends on the manager to hand-relay changes (e.g. a new feature
# set reaching QA). Agents still have full repo/DB access to dig further.
#
# Usage: scripts/team_brief.sh [--advance]   (--advance moves the review marker to HEAD,
# so the manager runs it ONCE per wake with --advance, then pastes the output to agents.)
set -uo pipefail
cd "$(dirname "$0")/.."
MARKER_FILE=".team_review_marker"
HEAD_SHA=$(git rev-parse --short HEAD)
if [[ -f "$MARKER_FILE" ]]; then BASE=$(cat "$MARKER_FILE"); else BASE=$(git rev-parse --short HEAD~15 2>/dev/null || git rev-list --max-parents=0 HEAD); fi

echo "### TEAM BRIEF — changes since last review (${BASE}..${HEAD_SHA})"
echo
echo "## Commits since last review:"
git log --oneline "${BASE}..HEAD" 2>/dev/null || git log --oneline -15
echo
echo "## Files changed (diff --stat):"
git diff --stat "${BASE}..HEAD" 2>/dev/null || true
echo
echo "## Feature sets currently in DB (a NEW version here = a feature-set change to vet):"
docker compose exec -T timescaledb psql -U quant -d quant -c \
  "SELECT version, array_length(names,1) AS n_features FROM feature_sets ORDER BY version" 2>/dev/null || echo "(db unavailable)"
echo "## feature_vectors by version (rows / dates):"
docker compose exec -T timescaledb psql -U quant -d quant -c \
  "SELECT set_version, count(*) AS rows, count(DISTINCT ts::date) AS dates FROM feature_vectors GROUP BY 1 ORDER BY 1" 2>/dev/null || true
echo "## Standing context to read: docs/QA_LEDGER.md (open concerns + invariants), STATE.md (plan), JOURNAL.md tail"

if [[ "${1:-}" == "--advance" ]]; then echo "${HEAD_SHA}" > "$MARKER_FILE"; echo "(review marker advanced to ${HEAD_SHA})"; fi
