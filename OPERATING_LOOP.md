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

## Task priority (when choosing what to do)

1. Fix anything broken (health).
2. Unblock/advance the current phase gate.
3. Hardening + tests + data-quality for what already exists.
4. Cleanup/reorganization/documentation.
5. Forward research/improvements from `docs/RESEARCH.md` (once infra supports them).
