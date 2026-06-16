# Observability — dashboards, the live latency picture, and what each panel tells you

> Status: ACTIVE (2026-06-16). Owner: standing latency & observability. Companion to
> `docs/LATENCY_PLAN.md` (the pure-compute sub-50ms target) and `docs/SIM_LATENCY_AUDIT.md` (the sim
> profiler). This doc is the operator's map: what we can SEE, where to look, and the thresholds worth
> alerting on. "You cannot improve what you cannot see."

## 0. TL;DR — the live latency picture (measured 2026-06-16, market open)

- **The metrics ARE exposed and scraped.** All 10 Prometheus targets are `up` (8 shard workers on
  `feature-computer:9201..9208`, the reader on `:9200`, prometheus itself). The "is the feature-computer
  exposing /metrics?" visibility gap is **already closed** — no capture change was needed.
- **End-to-end bar→vector latency is FEED-BOUND, not compute-bound.** The per-symbol drill-down
  (`latency_slow_symbols`) shows `arrival_lag_s ≈ 60.5–61.4s` tightly clustered across every shard and
  every minute, while `total_latency_s − arrival_lag_s` (OUR pipeline: bar-arrival → vector-ready) is
  **~0.5–0.7s**. Translation: **Alpaca delivers minute T's SIP bars ~60s after the minute closes**, and
  everything WE do on top of that adds well under a second. The `feature_assemble_seconds` /
  `feature_vector_latency_seconds` histograms reading ~60s (top bucket) is almost entirely this Alpaca
  delivery lag plus the reader's flush-on-next-minute boundary, **NOT** our compute.
- **Our pure per-minute compute is ~1.85s summed across groups per shard** (p50), dominated by
  `momentum_run`. That is the number `docs/LATENCY_PLAN.md` targets to drive < 50ms; it is real and
  improvable, but it is NOT today's bet-latency bottleneck — the feed is.
- **Top slow group: `momentum_run`** at ~750ms p50 / ~1000–2500ms p99 (it spikes). The window-sliced
  `compute_latest` override (the #27 follow-up) IS in the code on `main` and helps, but momentum_run's
  OLS-residual-skew + per-window run-length `list.eval` is intrinsically the most expensive group at
  production buffer depth. Next slowest: `breadth` (~344ms), `cross_sectional_rank` (~130ms),
  `residual_analysis` (~81ms), `price_returns` (~42ms).

**The honest operator conclusion:** optimizing compute improves the pure-compute tail (and the
sub-50ms goal) but will NOT move the bet-relevant end-to-end number until the ~60s Alpaca delivery lag
is addressed — and that lag is largely the provider's minute-bar delivery cadence, not ours. The
biggest latency *lever we control* on the bet path is therefore **tick-driven / partial-minute features
that do not wait for the minute's bar to be delivered**, not shaving compute milliseconds. See §4.

## 1. Dashboards (provisioned — load on Grafana restart)

All dashboards live under `grafana/provisioning/dashboards/*.json` and are auto-loaded by the `quant`
file provider (`provider.yml`). Two datasources are provisioned: **Prometheus** (uid `prometheus`) and
**TimescaleDB** (uid `timescaledb`, the default). Grafana at `http://localhost:3001`.

### Pre-existing (kept):
- `bar_to_vector_latency.json` — **Bar → vector latency (north star).** p50/p95/p99 end-to-end
  (first-bar anchor) + per-shard p99 + the pure-compute `feature_assemble_seconds` + per-group p99 + the
  `latency_slow_symbols` drill-down table.
- `feature_latency.json` — per-group compute p50/p99 (ms) timeseries + ranked "slowest groups now".
- `ingestion_rate.json` — bars/trades/quotes per second + cumulative + current-rate stat.
- `ingestion.json` — symbols reporting a 1-min bar (live coverage).

### New in this PR:

#### `strategy_lifecycle.json` — Strategy / bet lifecycle (smoke + reversion)
Reads `strat_smoke.bets` and `strat_reversion.bets` (Postgres).
| Panel | What it tells you | Source |
|---|---|---|
| Open bets / Open notional / Realized PnL today / Closed today | At-a-glance live exposure & today's result, per strategy | `status='open'`, `sum(entry_notional)`, `sum(realized_pnl)` |
| Cumulative realized PnL over time | Equity curve per strategy (stepped) | window `sum(realized_pnl) OVER (ORDER BY exit_ts)` |
| Bets placed / closed per 15m | Activity cadence — is the strategy actually trading? | `date_bin('15 min', entry_ts)` count |
| Recent bets (smoke / reversion) | Last 50 bets with entry/exit/PnL/(signal) for eyeballing | raw rows |
| Win rate & avg PnL per bet | Is the edge positive today? | `count FILTER (realized_pnl>0)` |

#### `feature_parity_health.json` — Feature & parity health
Reads `feature_trust`, `feature_validation_day`, `feature_validation_exception`,
`feature_parity_defect` (Postgres) + the live `feature_incremental_parity_breach_total` (Prometheus).
| Panel | What it tells you | Source |
|---|---|---|
| Open parity defects | **Must be 0.** Any DIVERGENT feature = a quarantined feature with a filed defect | `feature_parity_defect WHERE status='open'` |
| Features by value grade (pie) + Grade A count + Grade F/U count | Parity-certification coverage across the registered set | `feature_trust.value_grade` (A≥0.9999 … F/U) |
| Worst features by lifetime value-rate | Which features are closest to a parity break | `feature_trust.lifetime_value_rate ASC` |
| Daily parity value-rate (avg & min) | Trend — is parity drifting day over day? | `feature_validation_day.value_rate` |
| Recent validation exceptions | The actual diverging cells (stream vs backfill values) | `feature_validation_exception` last 14d |
| Live incremental-vs-batch parity breaches | The DEFAULT-OFF incremental fast path's live parity self-check; **any non-zero blocks flipping it on** | `feature_incremental_parity_breach_total` |

#### `pipeline_health.json` — Pipeline health & latency attribution
The operational "is it alive + where is the latency" view. Prometheus + `latency_slow_symbols`.
| Panel | What it tells you | Source |
|---|---|---|
| Capture alive? (bars/s) | Liveness — 0 during RTH = capture down | `rate(feature_bars_ingested_total[1m])` |
| Latest vector age (s) | Freshness — how stale is the newest computed minute | `now() − max(minute)` on `latency_slow_symbols` |
| Shards reporting (of 8) / Scrape targets up | Are all workers + scrape targets healthy | `count(up{job="feature-capture"}==1)`, `count(up==1)/count(up)` |
| Ingestion rate | bars/trades/quotes per second | ingestion counters |
| **LATENCY ATTRIBUTION: Alpaca delivery vs OUR pipeline** | **The key panel.** Separates the ~60s Alpaca delivery lag (not ours) from our sub-second pipeline so we never "optimize" the wrong thing | `latency_slow_symbols.arrival_lag_s` vs `total_latency_s − arrival_lag_s` |
| Bar → vector p50/p95/p99 | End-to-end histogram (includes Alpaca delivery) | `feature_vector_latency_seconds` |
| Slowest feature groups now (ranked) | Which group to optimize next | `feature_group_compute_seconds` p99 |
| Symbols reporting a bar per minute | Universe coverage sanity | distinct symbols in `latency_slow_symbols` |

> **Note on `feature_vectors` (Postgres):** the LIVE capture writes vectors to the parquet store
> (`/data/fp_store_real`), **not** the `feature_vectors` table (which the backfill/validation path uses
> and is empty during live capture). Live freshness is therefore read from `latency_slow_symbols`
> (written every minute by the drill-down) and the Prometheus ingestion rate — NOT from `feature_vectors`.

## 2. Alert thresholds worth setting

| Signal | Warn | Critical | Why |
|---|---|---|---|
| `sum(rate(feature_bars_ingested_total[1m]))` during RTH | < 5 | == 0 | capture/reader is down or disconnected |
| `count(up{job="feature-capture"}==1)` | < 8 | < 6 | a shard worker died → that shard's symbols stop computing |
| `feature_incremental_parity_breach_total` increase | any | any | parity is sacred — a breach must block the incremental fast path |
| `feature_parity_defect WHERE status='open'` | ≥ 1 | — | a feature diverged from backfill; investigate before trusting it |
| Latest vector age (`now() − max(minute)`) | > 120s | > 300s | pipeline stalled (distinct from the expected ~60–90s feed lag) |
| `our pipeline (arrival→vector)` avg | > 2s | > 5s | OUR compute/queue is backing up (the part we control) |
| `momentum_run` p99 | > 1.5s | > 3s | the dominant group regressed; check for a buffer-depth or feature-add regression |

## 3. The live latency numbers (2026-06-16, ~15:20 UTC, market open)

Per-group compute, p50 / p99 (ms), ranked, from `feature_group_compute_seconds` over a 10-minute window:

| group | p50 ms | p99 ms |
|---|---|---|
| momentum_run | 750 | 995–2457 (spikes) |
| breadth | 344 | 497 |
| cross_sectional_rank | 130 | 475 |
| residual_analysis | 81 | 242 |
| price_returns | 42 | 227 |
| technical | 38 | 96 |
| market_context | 34 | 191 |
| calendar_events | 21 | 133 |
| candlestick | 18 | 90 |
| (remaining ~26 groups) | ≤ 17 | ≤ 48 |

Sum of per-group p50 ≈ **1.85s** per shard per minute (serial). Per-symbol drill-down (top-K slowest):
`arrival_lag_s` (Alpaca) p50 **60.9s** (min 60.5, max 61.4); `total_latency_s − arrival_lag_s` (ours)
**~0.5s**. Conclusion restated: **the feed dominates by ~100×.**

## 4. Prioritized next latency wins

Ranked by impact on the metric that matters (bar→vector for that bet), most impactful first.

1. **Partial-minute / tick-driven feature path (the only real bet-latency lever).** The ~60s Alpaca
   minute-bar delivery lag is the dominant term and is the provider's cadence, not ours. The lever WE
   control is computing (a subset of) features from the trade/quote firehose as ticks arrive *within*
   the minute, rather than waiting for the minute's bar to be delivered ~60s late. Scope: identify which
   registered features are computable from intra-minute ticks (microstructure/trade_flow/quote_spread
   already consume raw ticks) and emit a partial vector at the minute mark from the firehose. Large,
   parity-sensitive — design doc first; not a one-PR change.
2. **`momentum_run` per-window run-length `list.eval` (the #27 pattern, taken further).** The
   window-slicing override is already deployed; the residual cost is the per-window
   `rolling().agg() → list.eval(min_horizontal(element, cum_count)).list.max()` repeated for each of
   `WINDOWS`. Vectorize the cap across windows in one pass (compute the global capped run once, slice per
   window) instead of a `list.eval` per window. Guarded for free by `tests/test_fp_latest.py`
   (`compute_latest == compute().filter(last minute)`). Expected: pulls momentum_run from ~750ms toward
   the residual_analysis tier. **Clearest pure-compute win.**
3. **Per-group buffer-depth slicing before `compute_latest` across ALL windowed groups** (the broader
   #27 lever named in `capture.py` as backlog P1.0). `breadth`, `cross_sectional_rank` and
   `residual_analysis` still over-feed a deep buffer to `compute_latest`. Apply the same window-slice
   helper (`compute_latest_on_window`) momentum_run uses, group by group, each guarded by
   `tests/test_fp_latest.py`. Cuts the recompute tax across the board.

This PR does NOT implement #2/#3 — they are parity-sensitive feature-math changes that each deserve a
focused PR + parity run (per CLAUDE.md "speed that breaks parity is a FAILURE"). They are listed here so
the next engagement picks the highest-value one with the attribution already done.
