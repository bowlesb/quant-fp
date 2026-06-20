# System Description

The top-level map of the quant-fp platform. The system is a **feature-platform-first** autonomous trading
research system: it captures market data, computes a large parity-true feature store in real time, lets
independent strategy containers consume those features off a decoupled bus, executes on a paper (later real)
broker, and is operated continuously by a team of long-lived agents.

This document is a **navigation layer**, not a re-explanation: each subcomponent gets a short description and a
link to its authoritative doc(s), which in turn link to their related docs — forming a navigable hierarchy.
For the platform vision and acceptance bar, start at [MISSION](MISSION.md) and
[FEATURE_PLATFORM](FEATURE_PLATFORM.md).

> The pre-pivot whole-system snapshot [`ARCHITECTURE.md`](../ARCHITECTURE.md) is **deprecated** (kept for
> history). This document supersedes it as the system map.

---

## The data path, end to end

```
Alpaca / EDGAR / news  ──►  raw tape (/store/raw)  ──►  feature compute (state-execution)  ──►  feature store (/store)
                                                                  │                                    │
                                                                  ▼                                    ▼
                                                          feature-vector BUS  ──►  strategy containers  ──►  execution (paper→real)
                                                                  │
                              trust / parity lifecycle ◄──────────┘            dashboard coverage grid + scorecard observe it all
```

---

## Subcomponents

### 1. Feature Platform & the feature store
The compute engine and the ~728-feature catalog it produces. Features are organized into ~63 groups across a
context taxonomy (price/volume, order-flow/microstructure, calendar, sector, cross-sectional, etc.), computed
identically for live and backfill so they are parity-true by construction.
→ **[FEATURE_PLATFORM](FEATURE_PLATFORM.md)** (vision, requirements, milestones FP0–FP4) ·
[FP_GOALS](FP_GOALS.md) (quantified scoreboard) · [FEATURE_TAXONOMY](FEATURE_TAXONOMY.md) ·
[FEATURES](FEATURES.md) (generated catalog) · [ADDING_A_FEATURE](ADDING_A_FEATURE.md) ·
[FEATURE_TOOLING](FEATURE_TOOLING.md) · [FEATURE_TYPES](FEATURE_TYPES.md) · [ORDER_FLOW](ORDER_FLOW.md) ·
[BREADTH_FEATURE](BREADTH_FEATURE.md) · [FUSED_ENGINE](FUSED_ENGINE.md)

### 2. State & incremental execution abstraction
How every feature group computes fast and stays parity-true: each group is either intraday-invariant (cached
once) or stateful (declares `seed`/`fold`/`emit` over a shared running-state object), and the engine owns the
fast paths. This is the substrate the latency work pushes more compute into.
→ **[UNIFIED_STATE_EXECUTION_SPEC](UNIFIED_STATE_EXECUTION_SPEC.md)** (the Class-A/Class-B contract) ·
[STATE_ABSTRACTION](STATE_ABSTRACTION.md) (FeatureState protocol) ·
[INCREMENTAL_INTEGRATION](INCREMENTAL_INTEGRATION.md) · [INCREMENTAL_READINESS](INCREMENTAL_READINESS.md)
(per-group audit: who rides running state, remaining levers) · [P3_RUST_RESIDENT_FOLD](P3_RUST_RESIDENT_FOLD.md)

### 3. Feature-vector bus & decoupling
Live feature vectors are published to a Redis-Streams bus; strategy containers resolve features **by name**
against the published schema, so producers and consumers no longer need coordinated fingerprint-locked deploys.
This is what lets the feature engine and strategies ship independently.
→ **[BUS_FEATURE_ACCESS](BUS_FEATURE_ACCESS.md)** (name-addressed, fingerprint-decoupled consume) ·
[STRATEGY_CONTAINERS](STRATEGY_CONTAINERS.md) · [MARKET_DATA_STREAMS](MARKET_DATA_STREAMS.md) (the raw
`md:bar`/`md:trades`/`md:quotes` channels alongside `fv:`)

### 4. Data, raw tape & backfill
The acquire→materialize→validate spine: raw Alpaca bars/trades/quotes are downloaded once into the raw tape,
features are materialized from it, and the result is validated against the live stream. The raw tape is the
reusable substrate for inventing features without re-downloading.
→ **[BACKFILL](BACKFILL.md)** (the three-stage spine) · [BACKFILL_SCOPE](BACKFILL_SCOPE.md) ·
[VECTOR_BACKFILL](VECTOR_BACKFILL.md) · [RAW_TAPE_COVERAGE](RAW_TAPE_COVERAGE.md) ·
[STORE_PROVENANCE](STORE_PROVENANCE.md) (the `source=` partition) · [M2_SHARDING](M2_SHARDING.md) ·
[STREAM_COMPACTION](STREAM_COMPACTION.md) · [MARKET_DATA_STREAMS](MARKET_DATA_STREAMS.md) ·
[CORPORATE_ACTIONS](CORPORATE_ACTIONS.md) / [CORPORATE_ACTIONS_PARITY](CORPORATE_ACTIONS_PARITY.md) ·
[CRYPTO_CAPTURE](CRYPTO_CAPTURE.md) (isolated 24/7 validation harness)

### 5. Trust, parity & validation lifecycle
The discipline that earns and keeps trust: a (feature, version) is binary TRUSTED or NON_TRUSTED, earned on a
clean day by proving live `compute_latest` == backfill `compute`, then re-checked continuously. Contamination-
aware grading and append-only ledgers keep the trust signal honest.
→ **[TRUST_REDESIGN](TRUST_REDESIGN.md)** (the binary, self-checking design of record) ·
[PARITY_LIFECYCLE](PARITY_LIFECYCLE.md) (T+1 settled-day test + contamination-aware grading) ·
[PARITY_COVERAGE](PARITY_COVERAGE.md) · [PARITY_PLAYBOOK](PARITY_PLAYBOOK.md) ·
[VALIDATION_LEDGER](VALIDATION_LEDGER.md) · [DATA_QUALITY_LEDGER](DATA_QUALITY_LEDGER.md)

### 6. Strategy execution & state layer
The production execution+state layer that turns a strategy's target weights into broker orders and survives
restarts: one append-only ledger is the source of truth, with idempotent client-order-id lifecycle and
restart-safe reconciliation, so the paper executor and the faithful backtest executor share one decision core.
→ **[STRATEGY_EXECUTION_ABSTRACTION](STRATEGY_EXECUTION_ABSTRACTION.md)** (the built layer) ·
[EXECUTION](EXECUTION.md) (Alpaca API reference + design)

### 7. Strategy research harness & edge hunt
The fast, production-portable way to test whether a (tickers × features × time) panel holds tradeable edge:
`run_strategy()` produces P&L + percentile diagnostics + anti-fooling baselines, and the same decision core
runs in both backtest and live. A pre-registered, multi-explorer edge hunt feeds it.
→ **[STRATEGY_HARNESS](STRATEGY_HARNESS.md)** (train→apply→evaluate) ·
[STRATEGY_BATTERY_RESULTS](STRATEGY_BATTERY_RESULTS.md) ·
[STRATEGY_BATTERY_PORTABILITY](STRATEGY_BATTERY_PORTABILITY.md) (shared-decision-core contract) ·
[RESEARCH](RESEARCH.md) · [EXPLORATION_PIPELINE](EXPLORATION_PIPELINE.md) · [MODELLING_AGENT](MODELLING_AGENT.md)

### 8. Dashboard & coverage grid
The LAN dashboard, now a single always-warm coverage grid (dates × raw-layer/feature-group columns, cells =
fraction of the universe covered, blue/red = trust), backed by a Mongo cache the worker refreshes every 10 min.
Plus the progress scorecard and the per-dimension coverage surfaces.
→ **[STORE_GRID](STORE_GRID.md)** (the coverage grid + its API) · [SCORECARD](SCORECARD.md) (six progress
axes) · [OBSERVABILITY](OBSERVABILITY.md) · [FEATURE_DASHBOARD](FEATURE_DASHBOARD.md) ·
[RAW_TAPE_COVERAGE](RAW_TAPE_COVERAGE.md) · [SECTOR_COVERAGE](SECTOR_COVERAGE.md) ·
[UNIVERSE_COVERAGE](UNIVERSE_COVERAGE.md) · [TICKER_REPRESENTATION](TICKER_REPRESENTATION.md)

### 9. Latency & accountability
The bar-arrival → vector-ready latency budget and the regime that keeps it honest: a streaming sim measures
per-group compute and end-to-end latency, profiled before risky changes, with a version-controlled budget the
team is accountable to over time.
→ **[LATENCY_PLAN](LATENCY_PLAN.md)** (the sub-50ms target + measurement regime) ·
[`latency_budget.yaml`](latency_budget.yaml) (the checked-in per-group budget) ·
[PROFILE_SIM](PROFILE_SIM.md) (pre-flight profiler) · [SIM_LATENCY_AUDIT](SIM_LATENCY_AUDIT.md) (baseline) ·
[INCREMENTAL_READINESS](INCREMENTAL_READINESS.md) · [SCALABILITY](SCALABILITY.md)

### 10. EDGAR & news ingestion
Point-in-time ingestion of SEC filings (EDGAR) and news as their own raw tapes, captured with an `available_at`
dissemination instant so any feature derived from them is look-ahead-safe.
→ **[EDGAR_INGESTION](EDGAR_INGESTION.md)** (the point-in-time ingestion design) · [EDGAR_PLAN](EDGAR_PLAN.md) ·
[NEWS_INGESTION](NEWS_INGESTION.md)

### 11. Agent operating model & ops
How the system runs itself: seven long-lived agent workstreams (each a charter + append-only ledger), a
continuous maintenance loop, a session warm-up before the open, and the operational runbook + safety rules
(live-capture golden rules, health checks, cron registry).
→ **[OPERATING_MODEL](OPERATING_MODEL.md)** (the seven workstreams) · [OPERATIONS](OPERATIONS.md) (runbook +
cron registry) · [OPERATING_LOOP](../OPERATING_LOOP.md) (root) · [RESPONSIBILITY_MAP](RESPONSIBILITY_MAP.md) ·
[ROLE_CHECKLISTS](ROLE_CHECKLISTS.md) · [MAINTENANCE_PROTOCOL](MAINTENANCE_PROTOCOL.md) ·
[SESSION_WARMUP](SESSION_WARMUP.md) · [VERIFICATION_CULTURE](VERIFICATION_CULTURE.md) ·
[PR_WORKFLOW](PR_WORKFLOW.md) · [REVIEW_POLICY](REVIEW_POLICY.md) · [QA_LEDGER](QA_LEDGER.md) ·
[TECH_DEBT](TECH_DEBT.md) · [AUTONOMOUS_BACKLOG](AUTONOMOUS_BACKLOG.md) · [ROADMAP](ROADMAP.md)

---

## Living state (where to look "now")
- **[STATE.md](../STATE.md)** — current build state.
- **[JOURNAL.md](../JOURNAL.md)** — append-only running journal.
- **[ROADMAP](ROADMAP.md)** — the FP0–FP4 ladder + downstream edge track.
- **[docs/progress/](progress/)** — the 8-hour cadence progress reports.
- **[EXPERIMENTS](EXPERIMENTS.md)** — the edge-hunt experiment ledger.

## Conventions
- **[CLAUDE.md](../CLAUDE.md)** — code standards + the completion bar (QA pipeline, commit discipline).
- **[MISSION](MISSION.md)** — the north star.
