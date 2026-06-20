# Operating Model — the seven workstreams

Authored 2026-06-19 from Ben's direction. This is the canonical map of HOW the platform is run: seven
workstreams, each a standing role with a clear mission, scope, and owner. The Lead (7) supervises 1–6,
budgets compute across them, synthesizes state to Ben, and runs the hourly status dashboard (§Dashboard).

Each standing role is a long-lived agent with a **charter** + an append-only **ledger** in `~/.quant-ops/`,
woken on a schedule with fresh context (reads its charter + ledger first). Code changes are worktree→PR,
reviewed/merged by the Lead; nothing changes the bus fingerprint or touches live capture without the Lead.

## Staying informed — the system log (EVERY agent, every cycle)

The loops are not silos — they run as a TEAM with a CONDUCTOR (the Lead). The coordination surface is ONE
shared, accumulating file: **`~/.quant-ops/SYSTEM_LOG.md`**, with three sections, all read by every agent:

1. **CURRENT STATE** (Lead-maintained) — the live whole-system picture: bus fingerprint, what's deploying,
   trusted cohorts, what each workstream is doing right now.
2. **GAPS & CROSS-TEAM CONSIDERATIONS** (Lead-maintained, refreshed every orchestration cycle) — the
   CONDUCTOR layer. Each cycle the Lead considers the ENTIRE team and writes the system's current gaps:
   things falling *between* workstreams, conflicts, places where one role can unblock or hand off to another,
   under-served areas. Each gap is TAGGED with the workstream(s) that can address it. This is how each agent
   gains "consideration across the entire team," not just its own role.
3. **ROLLING LOG** (every agent appends) — the accumulating change feed: one terse dated line whenever an
   agent ships/learns something system-relevant: `YYYY-MM-DDTHH:MMZ [workstream] — what changed + who it affects`.

**Every agent, at the START of its cycle, reads all three** — CURRENT STATE (awareness), GAPS (picks up any
gap tagged to or within its scope and may act on it), recent ROLLING LOG (what just changed). Then acts. So
awareness + cross-team gaps flow DOWN (Lead → shared state), work flows UP (ledgers + reports → Lead), and
the team state ACCUMULATES over time. Keep entries terse — signal, not journal.

**Anti-duplication / anti-cross-purposes (NON-NEGOTIABLE — without this the loops collide):**
- **CLAIM before you act.** Once an agent picks the task it will do this cycle, it FIRST appends a claim to
  the ROLLING LOG: `YYYY-MM-DDTHH:MMZ [workstream] CLAIMING — <task>`. Then it does the work, and on finish
  appends `… SHIPPED — <task> (PR #N)` (or `DROPPED — <reason>`).
- **Check claims first.** Before choosing a task, scan recent ROLLING-LOG claims: if the thing you were about
  to do is already CLAIMED (and not yet SHIPPED/DROPPED) by another agent, or is outside your scope, pick a
  DIFFERENT task. Never redo work another loop already claimed.
- **The Lead is the conductor + tie-breaker.** Each orchestration cycle the Lead refreshes GAPS to route work
  so roles hand off rather than overlap, resolves any contention it sees in the claims, and dedupes PRs at
  review. When two workstreams could do a thing, GAPS says which one owns it.

---

## 1. Alpaca Data Backfill (raw layer)

**Mission:** keep a complete, always-current raw tape — trades, quotes, minute bars — that callers can query
seamlessly without knowing which points came from real-time capture vs backfill.

- Backfill trades / quotes / minute bars INDEPENDENTLY of the features derived from them.
- Current coverage: trades ~1y, quotes ~2mo (est.), bars ~1y. Stay current every day as time passes.
- Do NOT double-acquire: if a day was already captured well by real-time, don't re-backfill it.
- Seamless historical reads: a single query spans real-time + backfill transparently; callers trust accuracy.

**Owner today:** the DIA (ops/raw_backfill.sh, the manifest, daily acquire). **Gap:** the daily acquire runs
pre-settle (writes stubs — fixed by #114/#116 self-heal; source fix = acquire settled D-1) and the
real-time-vs-backfill "no double acquire" seam is implicit, not explicit.

## 2. Feature Backfill & Lifecycle

**Mission:** own the feature lifecycle — untrusted → trusted → kept-current — and the scheduled jobs behind it.

- Features start UNTRUSTED (implemented on an experiment or a hunch that they might add value).
- Every TRUSTED feature is kept up to date and backfilled appropriately as each new day lands.
- Seamless feature reads: a query over [now → N days back] spans real-time + backfill transparently.
- Randomly re-check TRUSTED features on a well-thought-out schedule.
- Aggressively pursue moving UNTRUSTED → trusted (verify-first, then backfill).
- Decide/manage/run scheduled jobs; inspect job logs regularly; fix failures.
- Every job is PLANNED for memory/time/resources, and run only when its compute is justified vs other goals.

**Owner today:** the DIA. **Gap:** random re-check cron is specced (trust_random_check) but not yet on the
schedule; job-log review is ad-hoc, not a standing discipline.

## 3. Data Warehouse Manager

**Mission:** own the feature-store schemas, formats, and the human-facing coverage dashboard.

- Assess whether feature-store schemas are up to date and appropriate; review formats, repo versions,
  read/write patterns, whether data is written correctly. **Requests Lead permission before changes.**
- Build + maintain a fast, extensible React dashboard giving human transparency into feature coverage over
  time (which ticker has which feature, how far back).
- Watch ticker representation — flag tickers lacking adequate data.
- Works closely with the backfill jobs (1, 2) and features.

**Owner today:** none (a rough feature-grid dashboard exists). **Gap:** no standing role; coverage view is
thin; schema/format stewardship is unowned.

## 4. Parity Issue Manager

**Mission:** make every feature reproduce between real-time and backfill — and keep it that way.

- When a real-time-collected feature ≠ its backfill recomputation, PROACTIVELY fix it (PRs that make the two
  paths agree). Willing to tackle ANY parity issue for ANY feature.
- Manage trusted → untrusted transitions; investigate why a feature lost parity.
- Be intimate with the real-time optimizations + state managers; maintain parity while allowing seamless
  compute in either the backfill or the optimized real-time path.
- Make abstraction improvements to the real-time/backfill paths for understandability + reliability; own the
  trade-offs between the two settings; decide whether incremental state should be held in backfill too.

**Owner today:** ad-hoc (TickParity this session; the DIA files defects). **Gap:** not a standing role; the
defect backlog (currently ~507 from the 06-18 sweep, mostly a coverage artifact) has no dedicated owner.

## 4b. Latency of Production Features

**Mission:** drive bar-arrival → full-feature-vector toward <100ms, and kill latency regressions in real-time.

- Aggressively measure bar→vector E2E (feed-delivery + shard-compute + gather), p50/p99, per feature + overall.
- DETECT → FIX → VERIFY regressions immediately: a slow/regressed feature gets a worktree→PR same cycle, with
  the speedup re-measured. (Highest loop frequency for this reason.)
- Maintain/improve the real-time state abstractions so each minute bar is a few small incremental updates, not
  a window recompute (the incremental fast-path + the heavy-group migration — docs/LATENCY_PLAN.md §7).
- Create new monitoring/metrics; recommend system-efficiency improvements to the Lead.
- A feature whose latency is verified-fast becomes a candidate to be "trusted" for the low-latency path.

**Owner today:** a standing Latency loop (was LatencyOpt; #117 landed the gate, #118 the roadmap). **Gap:**
production runs the batch path (FP_INCREMENTAL off); the heavy-group migration is unstarted.

## 5. The Modeller

**Mission:** find the edge — disciplined research on the trusted feature store + raw tape, and propose the
features worth building.

- Recommend which feature backfills to prioritize.
- Opportunistically exploit EXISTING store data to explore ideas, even with data gaps.
- Read the literature; opportunistically implement a relevant, interesting paper; consider GPU fine-tunes.
- Organize + thoroughly DOCUMENT every experiment (code SHA, data state, features used) so any agent can
  replicate/extend it later as data + compute grow.
- Analyse features; run experiments that motivate NEW features; open PRs for new/improved real-time features.
- Hunt the edge at the intersection of our quote/trade streaming and novel real-time ML.

**Owner today:** the MA (research cycles) + TrainingSubstrate (the harness). **Gap:** experiment-replication
discipline (SHA + data-state stamping) is partial; the harness + research pipeline aren't fully unified.

## 6. Maintainer & Clean-up

**Mission:** keep the codebase understandable, organized, and free of dead weight.

- PRs that remove dead code, duplicative/outdated docs.
- Aggressively consolidate duplicated code WITHOUT changing functionality.
- Archive historical resources (old docs/scripts) into an `archive/` rather than delete; ask the Lead when
  unsure whether something is still needed.
- Review git history + PRs to stay informed of current state + running jobs.

**Owner today:** none. **Gap:** new role.

## 7. The Lead & Coordinator (this session)

**Mission:** orchestrate 1–6, budget compute, review/merge PRs, run production deploys, and be the single
synthesizer of state + blockers to Ben.

- Manage the web interfaces as one coordinated app: Grafana, the Feature-Store UI, the status dashboard.
- Budget compute across jobs/workstreams; start/stop production systems on new code; review PRs as they land.
- Know the state + blockers of 1–6; synthesize to Ben; proactively surface gaps + architectural improvements
  worth resourcing.
- Cleanly separate what needs Ben's attention (open-ended, value-laden, genuinely ambiguous) from what
  doesn't (manageable with limited attention).
- Take ad-hoc Ben requests; deliver directly or via a subagent.
- Manage the Claude agent loops so all workstreams stay continuously active during the day; ensure progress
  despite Claude token limits (never let a token wall permanently block a workstream).
- **Own the hourly status dashboard (§Dashboard).**

---

## The hourly status dashboard (Lead-owned)

A 24/7, hourly-updated web UI Ben monitors from work and reacts to during the day.

- **Shape:** a TABLE. A new ROW is added every hour (timestamp). Columns are the seven workstreams (1–7).
- **Each cell:** two concise fields — **Progress** (status + recent improvements) and **Blockers** (ONLY a
  legit problem the Lead can't confidently move past without Ben — for most workstreams, most hours: none).
- **Ben reaction:** an input box where Ben types a reaction to the hour's status; the Lead reviews these
  every cycle and acts on them.
- **Discipline:** Progress + Blockers are chosen carefully and kept concise. A Blocker means a real,
  attention-worthy problem — not routine work-in-progress.
- **Cadence:** updated every ~hour by the Lead's loop, synthesizing each workstream's ledger/state. The Lead
  reviews Ben's reaction inputs each cycle.

Build decisions (see the plan): hosting (extend the existing dashboard app vs standalone), persistence
(append-only store of rows + reactions), and how rows are generated (Lead-synthesized each cycle).

---

## Compute discipline (cross-cutting)

No job runs unless its compute (memory/time/resources) is planned AND justified against the business value
relative to other objectives. The Lead budgets compute across workstreams; jobs are memory-bounded
(sharded/`--processes 1` for the heavy tick materialize), logged, and monitored to completion. Verify-first:
don't spend deep-backfill compute on a feature until it's deterministic or parity-verified.

---

## Related docs
Part of the [System Description](SYSTEM_DESCRIPTION.md) → *Agent operating model & ops*. See also:
[OPERATIONS](OPERATIONS.md) · [RESPONSIBILITY_MAP](RESPONSIBILITY_MAP.md) ·
[MAINTENANCE_PROTOCOL](MAINTENANCE_PROTOCOL.md) · [VERIFICATION_CULTURE](VERIFICATION_CULTURE.md) ·
[ROADMAP](ROADMAP.md) · [AUTONOMOUS_BACKLOG](AUTONOMOUS_BACKLOG.md).
