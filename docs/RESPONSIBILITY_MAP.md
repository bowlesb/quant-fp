# Responsibility Map — who owns what (so nothing slips through the cracks)

Goal: a self-correcting, self-rebuilding system like a small company. That requires
EVERY core problem area to have a named owner who OWNS THE OUTCOME (verifies it's green
each cycle), not a narrow analyst who recommends and disappears. "Unowned" is a defect.

## Core areas, owners, and the invariant each owner VERIFIES every wake

| # | Area | Owner | Invariant the owner verifies (closed loop) |
|---|------|-------|--------------------------------------------|
| 1 | Data integrity/quality | **QA** | `docs/QA_LEDGER.md` invariants green (calendar, parity, PIT, warmup/coverage, no degenerate data) |
| 2 | Infra/reliability/lights-on | **Production Eng** | all services up; ingestion fresh; no DB contention; recovers on restart |
| 3 | ML/signal/features | **Modeller** | honest IC reporting (canary + sign-consistency); NO false-edge claims; experiment log current |
| 4 | Architecture + tech debt | **Architect** (in Prod) | `docs/TECH_DEBT.md` triaged; periodic core-rebuild scheduled; complexity paid down, not accreted |
| 5 | **Execution / trading / risk / P&L** | **(SEE OPEN DECISION)** | executor correct; caps + kill-switch bind from fresh broker truth; reconciliation matches; P&L truthful |
| 6 | **Release / deploy correctness** | **Production Eng** | RUNNING code == intended (rebuilt+restarted after edits); change verified end-to-end BEFORE its output is trusted |
| 7 | Cross-role handoffs + orphans | **Manager** | every handoff works (new feature → QA via team_brief); scans for unowned concerns each wake |

## Own-the-outcome rules
- **Close the loop:** an owner's job is not done at "I recommend X." It's done when the
  area's invariant is VERIFIED green (or a regression is filed). One-shot analysis that
  evaporates is the failure mode we're correcting.
- **No orphans:** the Manager scans this map every wake. Any concern with no owner is
  assigned on the spot; a cross-cutting concern defaults to the Manager until assigned.
- **Evidence, not vibes:** "green" means a query/test/log was actually run this cycle.

## Known cracks that motivated this map (live evidence)
- **Warmup/coverage** went unmonitored — no owner until QA invariant I4 was added.
- **Stale-code deploy** (2026-06-11): the experimenter ran experiments on OLD code and
  produced plausible-but-wrong v1.0.0 results under v1.1.0 ids. Nobody owned "running ==
  intended." → Area #6 created; Prod owns rebuild+restart+verify before trusting output.

## OPEN DECISION (needs Ben): who owns Execution/Trading/Risk (area #5)?
For a trading system this is the money surface and currently the biggest orphan.
Options: (a) a 5th dedicated role "Execution/Risk Engineer"; (b) fold into Production
Eng with an explicit execution mandate; (c) split — Modeller owns trade SHAPE, Prod owns
plumbing, no single risk owner (status quo, NOT recommended). Recommendation: (a).
