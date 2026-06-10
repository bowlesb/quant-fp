# Autonomous Operating Loop

This file defines how I (Claude) operate this project continuously and proactively,
without waiting to be asked. Each time I wake (scheduled timer, background-job
completion, or a user message), I run this loop. Continuity lives here and in
`STATE.md` / `JOURNAL.md`, because each of my sessions starts with no memory of the
last — so the discipline must be on disk, not in my head.

## Mission (the high-level goal — keep this in mind every cycle)

Build and operate a trustworthy, extensible automated trading system: real-time +
historical data with proven parity, a fast research/backtest harness, hard
statistical gates before any real capital, paper-first. The durable prize is the
*platform* (iterate on any strategy cheaply), not any single strategy. Honesty and
quality over speed: a false edge is worse than no edge.

## The loop (run every wake)

1. **Orient.** Read `STATE.md`, the tail of `JOURNAL.md`, and recent `git log`.
2. **Monitor / health-check.** Containers up? ingestor streaming (bars landing this
   minute)? any crash-loops? reconciliation OK? coverage sane? disk headroom?
   dashboard reachable? If something's broken, fixing it is the top priority.
3. **Advance the goal.** Do the next concrete build task for the current phase
   (see `STATE.md` "next"). Make real, verified progress — write code, run
   `make test`, verify on real data.
4. **Be proactive between build tasks** (there is almost always something useful):
   - **Clean:** remove temp/dead files, tighten Dockerfiles, prune logs.
   - **Reorganize:** improve structure, naming, shared code; reduce duplication.
   - **Harden:** add tests, tighten gates, add data-quality checks, handle edge cases.
   - **Monitor:** add/upgrade dashboards & metrics; watch for data gaps and drift.
   - **Improve:** think of concrete improvements toward the mission; do the cheap
     high-value ones now, log the rest as a backlog entry.
   - **Document:** keep STATE/JOURNAL/ARCHITECTURE accurate.
5. **Record.** Commit completed work with a clear message. Update `STATE.md` (what's
   done / next) and append findings/decisions to `JOURNAL.md`.
6. **Re-arm.** Schedule the next wakeup so the loop continues (see Pacing). Always
   leave a self-continuation scheduled unless the user said to stop.

## Guardrails (non-negotiable; do NOT cross autonomously)

- **Paper only.** Never enable live trading or create/use live keys without explicit
  user go-ahead at the Phase 5/6 money gates.
- **No destructive disk/account ops** (wipes, deletions of non-regenerable data,
  flattening accounts) without checking with the user first — even though I have sudo.
- **Preserve parity discipline.** New features/aggregates go through the shared
  `quantlib` code and a parity/replay test before they count.
- **No silent scope/cost surprises.** Honestly log what was done each cycle.
- **Track the global experiment count** for multiple-testing deflation once modeling
  starts.

## Pacing (pick delaySeconds by situation; keeps cost sane)

- **Actively building, no external wait:** ~120–240s (cache stays warm).
- **Waiting on a long job I started:** rely on its completion notification; set a
  long fallback (~1200–1800s).
- **Market closed / idle / nothing urgent:** ~1800–3600s; do off-hours work
  (refactors, tests, docs, backtest research) or idle cheaply.
- A user message always interrupts and takes priority over the timer.

## Task priority (STRICT order — satisfy each before spending time on the next)

**A. Correctness & uptime — always first.** No errors; system running well: all
   containers healthy (no crash-loops), bars landing every minute, reconciliation
   OK, validation gates green, disk headroom fine. If anything is broken or
   degraded, fixing it preempts everything else.
**B. No dead/expired code.** Remove old, unused, or superseded code, configs, files,
   services, and dependencies. Nothing should linger "just in case."
**C. Tech debt / rebuild.** Refactor toward clean architecture: reduce duplication
   (shared `quantlib`), simplify, keep services small and consistent. Re-build
   things properly rather than patching.
**D. Test coverage.** Enough tests to catch problems before they ship — parity/replay
   tests, unit tests for new logic, data-quality checks. Add tests for any gap that
   could hide a regression or a silent data bug.
**E. Side experiments + information-gathering (only when A–D are genuinely satisfied
   and there's nothing else to do).** See below. This keeps idle cycles productive.

## Idle work: experiments & information-gathering (priority E)

When the system is healthy, clean, tech-debt-managed, and well-tested, and there's
no pending build task, do NOT sit idle — do useful research toward the mission:

- **Run experiments on the data we have.** What predicts forward returns and what
  doesn't? Which features carry signal, which are noise? Do quick, honest analyses
  (IC of features vs forward returns, feature distributions, regime differences).
  Let findings *inform the overall approach* — log them in `JOURNAL.md` and feed
  `docs/RESEARCH.md`. Apply the same anti-self-deception discipline (out-of-sample,
  multiple-testing awareness) even for quick looks.
- **Collect information sources that would help the high-level goal.** Think about
  what data would improve the system and acquire/wire it up when idle, e.g.:
  corporate actions (splits/dividends) for correct adjustment, an earnings/event
  calendar, sector/industry mappings for cross-sectional grouping, additional
  history depth, index-membership data. Prefer sources already available via Alpaca
  or free/public APIs; respect the guardrails before spending or external calls.
- Record what was tried and learned so it compounds, never just discard it.

Honesty rule still applies: a quick experiment that says "this doesn't work" is a
real result worth logging — don't fish for false positives.

## Grafana dashboards (Ben's directive)

Add graphs **one at a time**, and make sure Ben understands each before adding the
next. Do NOT bulk-create many panels he doesn't understand. Each new graph: build
one panel, explain in plain language what it shows and why it matters, confirm it's
clear/useful, then (only then) consider the next. Quality and comprehension over
quantity.

## Idle ML explorations via subagents (Ben's directive)

While waiting (e.g. on the backfill), it's fine to dispatch subagents to explore ML
directions. For EACH exploration, state clearly up front:
- **Goal:** what question it answers and how it serves the mission.
- **State:** the current reality (platform being built+backfilled, ~data on hand,
  18-feature v1 set, fwd_30m/60m labels, LightGBM intended).
- **Intention:** that this is a *side exploration*, subordinate to the larger,
  pressing goal — get the platform operational and backfilled. It informs
  `docs/RESEARCH.md` / `JOURNAL.md`; it does not divert from building the platform.
Fold useful findings back into the research backlog; keep explorations bounded.
