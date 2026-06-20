# Quant Trading System — Architecture

> **DEPRECATED — pre-pivot snapshot (2026-06-10).** This document predates the
> 2026-06-12 pivot to the Feature Platform spine and no longer reflects the current
> direction (it describes a "~40 v1 feature" Phase 0–7 ladder; the live system is the
> 618-feature / 11k-symbol parity-true feature store). It is kept for historical
> context only. For the current whole-system map see **`docs/SYSTEM_DESCRIPTION.md`**; for the
> source of truth see `~/.quant-ops/SYSTEM_LOG.md` (live state), `docs/FEATURE_PLATFORM.md`, and
> `docs/MISSION.md`.

This document captured the original *what we are building and why*, designed to be
read cold by a new engineer (in practice, a fresh Claude session) with no other
context. Progress lives in `STATE.md`; experiment results live in `JOURNAL.md`.

## Goal

An automated trading system that monitors a large liquid universe of US equities
in real time, computes features identically live and historically, uses an ML
model to rank symbols by expected short-horizon relative return, and places many
small capped trades. Paper-first, with hard statistical gates before any real
capital. Runs 24/7 on a single Intel box; operated autonomously across sessions.

## Committed strategy

**Cross-sectional, short-horizon ML ranking.** We do not predict whether a single
stock goes up. Every N minutes we predict, for each symbol in a ~1,000-name liquid
universe, its *forward excess return versus the universe* over a fixed horizon
(initially 30 minutes and overnight close-to-open). We go long the top decile and
short the bottom decile of predictions above a confidence threshold, holding many
small positions simultaneously.

Why this shape:
- Relative ranking is more forgiving than directional prediction — market beta
  cancels across longs and shorts.
- Many small bets per day → thousands of samples/month → we can separate edge from
  luck in weeks, not years. The acceptance gates depend on this.
- $100K has negligible market impact, so edges too small for funds to bother with
  are accessible to us. That smallness is our only structural advantage.

**Honest failure mode:** the most likely outcome is that any signal we find nets
~zero after spreads and slippage. The plan is therefore a series of kill/promote
gates that measure *after-cost* economics with our own quote data, and an escape
hatch of lengthening the horizon (30min → overnight → multi-day) where costs
shrink relative to signal. The architecture is identical at every horizon; only
the label and exit logic change.

## Non-negotiable design rules

1. **One feature codebase, two callers.** The exact same feature functions run in
   the live engine and the historical backtest builder. This is the single most
   important decision — it kills lookahead bias and train/serve skew at the root.
   Enforced by a replay-equivalence test (Phase 2 gate).
2. **Feature code never reads wall-clock time.** All time-based logic uses the
   point-in-time timestamp of the bar being computed, so any feature is
   reproducible for any historical instant.
3. **Append-only, fully reconstructable history.** Every bar carries its `source`
   (`stream`/`backfill`/`repair`) and `ingested_at`; we never overwrite, we append
   and compare. Every order stores the feature snapshot and prediction the model
   acted on, plus the actual fill. For any trade we can answer: what did the model
   see, what did it predict, what did we send, what filled, at what slippage.
4. **Point-in-time universe.** Universe membership is stored per day, so backtests
   use the universe as it was, not today's survivors.
5. **Paper vs live is one env flag** (`MODE=paper|live`). Nothing else differs.
6. **The trading loop never depends on a human or on Claude being present.** Kill
   switches, stale-data auto-halt, demotion rules, and gate criteria are enforced
   by the machine. Claude's absence can only slow research, never create risk.

## Deployment

- **Intel box (primary):** 32 threads, RTX 3090, 92GB RAM, 4TB NVMe SSD, Ubuntu,
  24/7. Runs the entire Docker Compose stack: ingestion, TimescaleDB, feature
  engine, training, backtesting, live loop, Grafana/Prometheus.
- **NAS (backup tier only):** nightly `pg_dump`/WAL archive + config snapshots.
- **3090:** not on the critical path. Reserved for a Phase 7 challenger-model lane
  (sequence models) and news embeddings. LightGBM on CPU is the committed model.

## Data layer

```
Alpaca SIP websocket ─┬─ 1-min bars ───────────────► bars_1m            (permanent)
                      ├─ quotes ── NBBO aggregates ─► quote_agg_1m       (permanent)
                      ├─ trades ── tick aggregates ─► trade_agg_1m       (permanent)
                      │              └─ raw ticks ──► trades_raw         (30-day rolling)
                      └─ news ─────────────────────► news               (permanent, small)
Alpaca REST (historical) ─► same tables via backfiller; same aggregation code.
```

Raw ticks are too large to keep forever (~0.5–1 TB/yr for the universe), so we
aggregate per-minute in-flight and keep raw ticks only 30 days for debugging and
developing new aggregates. Historical backfill streams REST tick data through the
*identical* aggregation code.

## Service topology (Docker Compose)

| Service | Role |
|---|---|
| `timescaledb` | Postgres 16 + TimescaleDB. All bars, aggregates, features, predictions, orders, fills, P&L. |
| `ingestor` | Alpaca websocket → bars/quote-agg/trade-agg/news → DB. Backpressure-aware. |
| `backfiller` | Historical REST bars/trades → DB through the same aggregation code. (Phase 1) |
| `feature-engine` | Shared feature library; live per-minute compute + historical store. (Phase 2) |
| `model-server` | LightGBM inference on the live feature vector. (Phase 3/4) |
| `portfolio-risk` | Sizing, caps, gross-exposure limit, kill switches. (Phase 4) |
| `executor` | Orders, fills, reconciliation against Alpaca `/positions`. (Phase 4) |
| `scheduler` | Cron: nightly streamed-vs-REST verify, gap repair, weekly retrain, daily report, NAS backup + restore drill. |
| `status` | Read-only JSON status endpoint (metrics only, no secrets, no controls). |
| `prometheus` + `grafana` | Observability + dashboards-as-code. |

## Phases and gates

- **Phase 0 — Foundation.** Stack, secrets, schema, paper connectivity, status +
  alerts, a hello-world loop (stream a few symbols → DB → one paper order/day →
  logged fill). Gate: survives reboot unattended; 5 days clean bars; daily order
  reconciled; induced-failure auto-recovery; daily report delivered.
- **Phase 1 — Full data layer.** SIP bars+trades+quotes+news for ~1,000 symbols;
  6yr backfill; nightly streamed-vs-REST validation. Gate: ≥99.5% symbol-minutes
  captured; streamed==REST ≥99.9%; backfill verified vs corporate actions; <5s bar
  latency p99. No model code until this passes.
- **Phase 2 — Feature engine + store.** ~40 v1 features, dual-run. Gate:
  replay-equivalence ≥99.9% match between live and recomputed; lookahead audit;
  adding a feature is a one-module change.
- **Phase 3 — Modeling + honest backtest.** Walk-forward purged/embargoed LightGBM;
  cost model from our own quotes. Gate (first kill/pivot): OOS IC ≥0.02 t≥4;
  after-cost Sharpe ≥1.0; profit survives +50% cost and ±1-bar delay sensitivity.
- **Phase 4 — Live paper shadow loop.** Full pipeline on paper. Gate: 15 days
  unattended, no recon breaks; backtest-vs-live fidelity ≥90%; kill-switch drill.
- **Phase 5 — Paper profitability campaign (≥8 weeks, money gate).** Frozen
  strategy. Gate: after-cost positive with t≥2; Sharpe ≥1.0; live inside backtest
  distribution; zero risk-limit violations. Ben makes the go-live call.
- **Phase 6 — Graduated live capital.** $10K → $40K → full, with automatic
  demotion rules. Indexed core holds the majority of capital throughout.
- **Phase 7 — Ongoing research.** Champion/challenger retraining; new feature
  families and strategy species (crypto funding carry, options) each run through
  the same gauntlet. Global experiment count tracked to deflate multiple-testing.

## Autonomy / continuity model

Each Claude session starts fresh, so continuity must live in the repo:
- **`STATE.md`** — current phase, what's done, what's running, what's queued next.
  Read at session start; updated at session end.
- **`JOURNAL.md`** — append-only experiment log: hypothesis, config hash, OOS
  result, verdict, next step.
- The **scheduler** runs standing jobs without Claude. Research is enqueued as
  config files and executed overnight; sessions read results and queue the next
  batch.
