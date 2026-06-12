# Review & Attribution Policy (Ben's directive, 2026-06-12)

One repo, role-attributed commits, tiered review. Goals: (1) Ben can review each agent's
contributions AND thought process at any time; (2) cross-agent review is structural on the
risky tiers, not luck; (3) exploration cadence stays friction-free.

## Attribution (every agent, every commit — no exceptions)

1. **Commit author = your role.** Always commit with:
   `git commit --author="<role> <role@quant-team>" ...`
   where `<role>` ∈ {manager, qa, modeller, prod-architect, execution-risk}.
   Use the ROLE name, not the session name (qa-2 commits as `qa` — sessions are mortal,
   roles are durable). Ben reviews contributions with `git log --author=<role>`.
2. **Subject prefix stays:** `qa:`, `modeller:`, `prod:`, `exec:`, `Manager:`.
3. **Thought process lives in your ledger.** Any non-trivial commit's WHY must be traceable
   to a ledger entry (QA→QA_LEDGER.md, Modeller→EXPERIMENTS.md, Prod→TECH_DEBT.md,
   Exec→EXECUTION.md, Manager→JOURNAL.md). The commit does the WHAT; the ledger does the
   WHY/alternatives-considered/evidence. Ben reads ledger + `git log --author` together.
4. **Helper:** `scripts/contributions.sh <role> [n]` — shows a role's last n commits with
   stats + points to its ledger.

## Review tiers

**Tier 1 — PR + named cross-agent review BEFORE merge (Manager merges):**
Code that can lose money or silently corrupt data:
- `services/executor/` and anything in the order/position/P&L path
- `quantlib/` (the shared live==backfill parity code) and feature/label definitions
- `services/model-server/`, model deploy/serving path
- `services/ingestor/`, `services/scheduler/`, `services/backfiller/`, `services/backfill-manager/`
- `docker-compose.yml`, Dockerfiles, Makefile targets that touch service runtime

Workflow: role branch `<role>/<topic>` → push → open PR (gh pr create) → request the
mapped reviewer (below) via SendMessage with the PR link → reviewer reviews ADVERSARIALLY
in their own lane's terms and approves/objects in a PR comment → Manager merges.
Squash-merge keeps history clean; the PR records who built, who vetted.

**Reviewer map (by what's at stake, not by politeness):**
| Change touches                     | Required reviewer |
|------------------------------------|-------------------|
| Order placement, caps, kill-switch, reconcile, P&L | execution-risk |
| Data semantics, adjustment/calendar/parity, invariants’ subjects | qa |
| Feature/label definitions, training, model artifacts | modeller |
| Service runtime, images, concurrency, schedulers, DB schema | prod-architect |
Two areas touched → two reviewers. Reviewer ≠ author.

**Tier 2 — direct commit to main (as today), attribution rules still apply:**
- Ledgers, JOURNAL, docs/, MARKET_DAY_PLAN
- `experiments/` (the Modeller's sandbox — review friction here kills the exploration
  mandate; gates apply only when something is PROPOSED for production, which is Tier 1)
- `tests/`, probe scripts in `scripts/` (QA's lane; they're read-only against the system)

**Incident fast-path:** for a declared live incident (broken collection, trading-path
fault, data corruption in flight), the owner may commit Tier 1 directly with subject
prefix `HOTFIX:`; the mapped reviewer reviews AFTER, within the same day, and the review
outcome is logged in the PR-equivalent: a ledger entry + Manager journal line. Fast-path
abuse (using HOTFIX for non-incidents) is a P1 process regression.

## Shared-worktree staging discipline (added 2026-06-12 after the b856aa7 absorption incident)

All agents share ONE working tree, so unstaged WIP from one agent is visible to all:
- **NEVER `git add -A`, `git add .`, or `git commit -a`.** Stage EXPLICIT paths only —
  files YOU changed for YOUR commit. b856aa7 ("prod #20") silently absorbed 66 lines of
  execution-risk's un-reviewed Tier-1 executor WIP this way.
- Before committing, `git status` and confirm every staged file is yours and on-topic.
  A file you don't recognize = a peer's WIP — leave it alone and flag them.
- If you absorb peer WIP anyway: do NOT deploy/rebuild the affected service; report to
  the Manager; default remedy = post-hoc review-before-deploy by the mapped reviewers.

## Manager duties under this policy
- Merge Tier 1 PRs only after the mapped reviewer's explicit approval.
- Keep this policy current; re-check compliance every wake (spot-check `git log` authorship).
- Report contribution summaries to Ben on request: per-role commits + ledger highlights.
