# PR Workflow — evidence-based changes, reviewed before merge

Every code change reaches the trunk through a reviewed PR with **proof**. The reviewer (the lead agent or
the owner) merges; the author never self-merges to the deployed line. This exists because un-reviewed,
un-evidenced "I fixed it" changes repeatedly turned out broken in production.

## THE EVIDENCE RULE (non-negotiable — a PR without this is rejected unread)
A PR is **not** a description of a fix. It is **proof**, with real command output:

- **BEFORE** — the actual broken/old state: paste the REAL command + its REAL output (the error, the
  `0 files`, the wrong value, the failing test, the NaN%). Not a sentence describing it — the actual output.
- **CHANGE** — what you changed and the one-line why.
- **AFTER** — the SAME command(s) re-run, with their REAL output now showing the fixed state (the value
  populated, the test passing, the file produced, the metric moved).
- **VERDICT** — honest: what is proven, what is NOT yet verified, what you did not test. If you could not
  verify it live (e.g. cold buffer, after-hours), SAY SO and say what still needs checking.

You may NEVER claim "fixed and working" on the strength of a code description alone. The merge decision is
made on the BEFORE/AFTER output, not on your prose. If you cannot produce AFTER evidence, the PR is a
DRAFT that says so — it does not assert success.

Examples of FORBIDDEN PR bodies: "Fixed the null bug, it works now." / "Added the guard, tests should pass."
Examples of REQUIRED: a `BEFORE` block showing `efficiency_ratio_5m: 0/4737` and an `AFTER` block showing
`efficiency_ratio_5m: 3359/4260` from the same query, plus the parity test output.

## How authors (subagents) make a change
1. **Work in an ISOLATED worktree** (you are spawned with one) — never edit the shared `/app` working tree.
2. **Capture BEFORE** — run the real command that demonstrates the issue; save its output verbatim.
3. **Make the smallest change** that addresses it. Follow CLAUDE.md (absolute imports, types, no nested fns,
   parity for feature changes). For a feature-value change, run the parity tests (`tests/test_fp_latest.py`,
   `tests/test_fp_stateful_emit.py`) and include their output.
4. **Capture AFTER** — re-run the BEFORE command(s); save the output showing the new state. Run `ruff check`
   on changed files; include the result.
5. **Branch + push:** `git checkout -b <type>/<short-desc>` (type = fix|feat|docs|ops), commit with a clear
   message ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`, push:
   `git push origin HEAD`.
6. **Open the PR** (or, until a GitHub token is configured, return the PR body to the lead): title +
   the BEFORE / CHANGE / AFTER / VERDICT body. With `gh`: `gh pr create --base main --title "..." --body "..."`.
7. **Return to the lead:** the branch name, the PR number/URL (or the full PR body), and a one-line summary.

## How the reviewer (lead) handles it
1. Read the diff (`git diff main..<branch>` / `gh pr diff`) AND the BEFORE/AFTER evidence.
2. Reject if: no evidence, evidence doesn't actually show the fix, parity not shown for a feature change,
   scope creep, or the change isn't reversible/safe.
3. Re-verify independently when cheap (re-run the AFTER command yourself).
4. Merge only when the evidence proves it: `gh pr merge --squash` (or `git checkout main && git merge --ff`).
5. Deploys that change running behavior follow the SEQUENCING + COLD-BUFFER rules in
   `docs/VERIFICATION_CULTURE.md` — done = deployed + verified live, not merged.

## Lane / isolation discipline
- One agent per workstream; isolated worktree per agent — this is what prevents the concurrent-working-tree
  corruption that has bitten us. The shared engine has a single owner; don't edit it from two PRs at once.
- Base branch is `main`. Keep PRs small and single-purpose so the evidence is unambiguous.
