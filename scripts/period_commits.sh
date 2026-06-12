#!/usr/bin/env bash
# List commits in a time window, grouped by role author — feeds the period progress report.
# Usage: scripts/period_commits.sh "2026-06-12 06:00" "2026-06-12 14:00"
set -euo pipefail
SINCE="${1:?usage: period_commits.sh <since> <until>}"
UNTIL="${2:?usage: period_commits.sh <since> <until>}"
for role in manager prod-architect modeller qa execution-risk; do
  COMMITS=$(git log --author="${role}" --since="${SINCE}" --until="${UNTIL}" \
    --pretty=format:'  %h %s' --date=local 2>/dev/null)
  if [[ -n "${COMMITS}" ]]; then
    echo "${role}:"
    echo "${COMMITS}"
  fi
done
