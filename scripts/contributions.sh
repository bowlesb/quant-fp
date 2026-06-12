#!/usr/bin/env bash
# Show a role's contributions: commits (git log --author) + its thought-process ledger.
# Usage: scripts/contributions.sh <role> [n_commits]
set -euo pipefail
ROLE="${1:?usage: contributions.sh <role> [n]}"
N="${2:-20}"
declare -A LEDGERS=(
  [qa]="docs/QA_LEDGER.md"
  [modeller]="docs/EXPERIMENTS.md"
  [prod-architect]="docs/TECH_DEBT.md"
  [execution-risk]="docs/EXECUTION.md"
  [manager]="JOURNAL.md"
)
echo "=== Commits by ${ROLE} (last ${N}) ==="
git log --author="${ROLE}" -n "${N}" --stat --date=format:'%m-%d %H:%M' \
  --pretty=format:'%C(yellow)%h%Creset %ad %s'
echo
LEDGER="${LEDGERS[$ROLE]:-}"
if [[ -n "${LEDGER}" && -f "${LEDGER}" ]]; then
  echo "=== Thought process: ${LEDGER} (tail) ==="
  tail -n 60 "${LEDGER}"
else
  echo "(no ledger mapped for role '${ROLE}')"
fi
