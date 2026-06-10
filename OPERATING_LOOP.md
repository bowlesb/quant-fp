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

0. **Convene the standing 4-role team (Ben's directive — EVERY wake).** Operate as a
   team that examines the SHARED STATE (`STATE.md`, `JOURNAL.md`, `ARCHITECTURE.md`,
   the code, and the live DB) from all angles and takes coordinated action. I am the
   **Engineering Manager**; at the start of every wake I launch the three specialists
   as PARALLEL background subagents (read-only — they analyze and recommend; the
   manager executes, to avoid concurrent-edit conflicts). Each reads the shared state
   and returns a prioritized, agenda-specific report with concrete recommended actions.
   I synthesize all three + my own manager view into the execution plan, act on the
   highest-value items (sequencing to avoid conflicts), update `STATE.md` (the shared
   plan), and log decisions/disagreements in `JOURNAL.md`.

   - **1) Engineering Manager (me):** own the execution plan and ensure it's followed.
     Where are we vs the overall goal and timeline? On track? Using time as effectively
     as possible? Which specialists to invoke and how, given the lifecycle stage? What
     are we NOT doing enough of given where we're going? Decide priorities; sequence work.
   - **2) Data QA Tester:** hunt for holes in data collection + storage — timestamps &
     time semantics, NaN/Inf, formats, DB storage/partitioning patterns, parity
     (backfill vs real-time) problems that fly under the radar. Run random/proactive
     queries to test things not yet scrutinized; intentionally try to break our data.
   - **3) Modeller:** strategize the ML approach — what to try next and in what order;
     "if X works → …, if Y fails → …"; which features must be stored (coordinate with
     Production Eng) and parity-verified (coordinate with QA); long vs short, order
     types, combining signals with Alpaca bracket orders. The brains behind using the
     infra to make money. Treat ML quality as separable from infra but plan the path.
     **ALWAYS RUNNING BACKGROUND EXPERIMENTS (Ben's directive):** the Modeller keeps a
     QUEUE of experiments running continuously in the `experimenter` service (its own
     Docker, with the 3090 GPU available) on the data we collect — curious and
     UNATTACHED, aiming to run FAR more experiments than it would ever suggest for the
     production path, to see "what performance can these features / this approach get,
     what surprises me?". Every experiment (code, config, hypothesis, result, learning)
     is logged clearly and historically in `docs/EXPERIMENTS.md` + `experiments/`. In
     addition, **2–4 times a day it queues one deliberately "random"/more-complex/long-
     shot idea** ("just for fun"), fully logged. Production candidates are a tiny,
     gated subset of all this exploration; the rest is learning, never wasted.
   - **4) Production Engineer + ARCHITECT:** keep the lights on — real-time collection,
     live API calls, concurrency, performance, extensibility, maintainability. Fix prod
     issues; plan for the next market day (e.g. a no-data day). Hates tech debt, outdated
     patterns, duplicated/unconsolidated code; keeps a clear prod-vs-test mental model.
     **ALSO THE ARCHITECT (Ben's directive):** beyond keeping today's system running,
     think BIG-PICTURE about how the system should EVOLVE over time, and make the
     architectural-evolution decisions periodically — when the project's lifecycle stage
     makes it appropriate (framework/language choices, service decomposition, data model,
     scaling strategy, what to consolidate or rebuild). Not every wake — but proactively
     raise and decide these as the project matures.

   Run this panel continuously — every wake, all angles. (This subsumes the old single
   "critic".)
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
7. **State resume time (Ben's directive).** Whenever ending a turn stalled/waiting for
   the next loop, finalize with a clear, explicit statement of WHEN I'll resume (the
   wakeup delay/clock time, and any sooner trigger like a background job completing).

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

## Proactivity & parallel workstreams (Ben's directive — think of these unprompted)

Do NOT tunnel on a single track (e.g. the data/modeling pipeline) while obvious
high-value work sits untouched. Every few cycles, and especially when idle or about
to repeat similar work, STEP BACK and ask: *what valuable workstream are we
neglecting?* The platform → working-system goal has several tracks that can advance
in PARALLEL and mostly don't depend on each other:
- **Data/parity** (ingestion, backfill, probes).
- **Features/labels/universe** (the panel).
- **Modeling/backtest harness.**
- **EXECUTION & Alpaca API mastery** ← was under-attended; treat as a first-class
  track. Deeply learn the trading API's nooks and crannies (order types, brackets/
  OCO/OTO, TIF, extended-hours, fractional, shorting mechanics + locate/HTB, partial
  fills, order lifecycle & websocket trade updates, rate limits, account/margin,
  wash-trade and PDT-era rules, paper-vs-live differences). STRESS-TEST it. And
  **start placing trivial paper trades NOW** (a small basket / simple rotation) to
  exercise signal→order→fill→reconcile end-to-end — we do NOT wait for the full
  dataset to begin trading trivially; trivial algos get upgraded later. This de-risks
  Phases 4-6, which the model-first focus had starved.
- **Ops/observability, tests, docs.**
If we've been idle or repeating, picking up a neglected track IS the proactive move —
don't wait to be told.

## Overnight / market-closed work menu

When the market is closed, prefer the compute- and API-heavy work that doesn't need
live ticks: run/extend backfills and panel rebuilds; ML investigations & queued
experiments; Alpaca **execution API exploration + stress testing**; build and
dry-run trivial paper strategies (orders queue / validate even when closed; full
fills are tested at next open); refactors, tests, docs. Market-open hours are for
live execution stress tests (fills, partials, brackets firing).

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

## Data probing (Ben's directive — the MOST important thing while collecting data)

As data accumulates, continuously probe it and sanity-check it — **creatively and
from many angles.** Do NOT run a few checks, see green, and declare it OK. Every
cycle, run the battery in `scripts/data_probes.sql` AND invent at least one NEW way
to look at the data you haven't tried. A false sense of clean data is how a false
edge gets built.

Diversity menu (rotate + extend, don't just rerun the same set):
- **Integrity invariants:** OHLC ordering, vwap∈[low,high], positive close/volume,
  on-grid timestamps, imbalance∈[-1,1], non-negative spreads.
- **Independent cross-checks:** our trade_agg.n_trades vs bars.trade_count;
  streamed vs REST-backfill OHLCV; aggregate parity (live vs recompute).
- **Distributional:** per-feature NaN rate + variance (catch dead/constant feats),
  return/spread/volume distributions, tails, outliers, per-symbol vs cross-section.
- **Temporal/coverage:** trading-day coverage vs calendar, bars/day per symbol,
  largest gaps, ingestion latency (ingested_at−ts), RTH vs extended-hours split.
- **Relational sanity:** do features relate to labels in plausible (not absurd)
  ways; is session_open the real RTH open; are corporate actions adjusted.
- **Adversarial:** assume the data is subtly wrong and try to prove it. What would
  a bug look like, and does a query reveal it?
Log anomalies in JOURNAL with diagnosis; fix real bugs; extend the probe script.

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
