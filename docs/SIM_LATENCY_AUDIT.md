# Simulation realism + latency audit (2026-06-15)

> An honest audit of the streaming sim's realism and the measured bar→vector latency, with the slowest
> groups named. Companion to `docs/PROFILE_SIM.md` (the pre-flight tool) and `docs/LATENCY_PLAN.md`.
>
> **Re-measured 2026-06-18 (682 features / 51 groups). See the dated section at the bottom for the current
> per-group baseline — the table just below is the original 519-feature 06-15 reading, kept as history.**

## TL;DR

- **The sim is realistic in SHAPE.** It already emits minute **bars + trades + quotes** through a
  protocol-faithful msgpack Alpaca mock, driven by the *real* `alpaca-py` `StockDataStream` — the exact
  Monday client. Message keys match what `capture.py` expects (`S,o,h,l,c,v,t` for bars; the Alpaca
  trade/quote shapes). The tick path (trade_flow / quote_spread / liquidity / tick_runlength) is actually
  exercised, not bypassed (pinned by `tests/test_fp_stream_sim.py`).
- **Two realism gaps (now addressed / documented):**
  1. The sim's `_report` measured **per-shard compute only** — it never measured the true
     **bar-arrival → universe-vector-ready** wall-clock (LATENCY_PLAN item 6, "end-to-end sanity"). The
     new `profile_sim.py` adds it.
  2. `stream_sim.main` defaulted to **5 trades + 5 quotes/min** — a token rate. The mock's own realistic
     default is 24t + 72q/min/symbol; `profile_sim.py` uses the realistic firehose by default.
- **Latency is well over budget at scale, and the cause is now named.** End-to-end bar→vector p99:

  | universe / shards | per-shard compute p99 | end-to-end bar→vector p99 |
  |---|---|---|
  | 200 / 4   | ~158ms | (small) |
  | 1000 / 16 | ~447ms | **~603ms** |
  | 3000 / 32 | ~1468ms | (proportionally higher) |

  Budget is ~100ms. **FAIL** at every realistic scale on the full 519-feature flow. The **fast path**
  (305 reduction features only) is ~56ms p99 at 200/4 and ~115ms p99 at 1000/16 — close to budget; it is
  the **non-reduction "rest" groups that blow it.**

## The slowest groups (the lever)

A single uncontended shard (93 symbols, 300m buffer) via `profile_sim`'s per-group ranking, independently
cross-checked by `python -m quantlib.features.profile 93 300 250 5 --latest`:

| group | per-shard p50 | note |
|---|---|---|
| **`momentum_run`** | **~95–230ms** | **dominant.** ~23,500 µs/feature — by far the worst per-feature cost |
| `residual_analysis` | ~13–50ms | second |
| `market_context` | ~12–25ms | cross-sectional gather |
| everything else | ≤ ~10ms each | |

### Root cause

`momentum_run` and `residual_analysis` use the **base-class `compute_latest`**, which runs the full
`compute()` over the **entire 300m trailing buffer every minute** and then filters to the last minute.
`momentum_run` only declares a 60m window, so ~80% of that buffer work is recompute it never uses, and its
`compute` does several `with_columns` passes over the whole `300m × N` frame plus a per-window
`rolling().agg()` with a `list.eval` — the expensive part. This violates the standing rule that *"a
feature earns its place only if it is timed and fast"* (`docs/LATENCY_PLAN.md`, `tests/test_fp_latency.py`).

### Recommended fix (NOT done in this PR — parity-sensitive, scoped separately)

Override `compute_latest` on `MomentumRunGroup` (and then `ResidualAnalysisGroup`) with an aggregate-at-T
form that:

1. slices the buffer to each feature's **own window** (≤60m for momentum_run) before computing, and
2. emits **one row per symbol** for minute T instead of the whole rolling frame.

This is exactly the pattern the reduction/stateful groups already use, and it is **guarded for free** by
`tests/test_fp_latest.py` (the generic invariant `compute_latest == compute().filter(last minute)`), so a
fast live form can never silently diverge from the backfill rolling form. Expected impact: the dominant
~95–230ms/shard term collapses toward the ~10ms tier, pulling the full-flow p99 toward the fast path's
sub-150ms. It is left out of this audit PR because momentum_run's OLS-residual-skew + run-length math is
parity-sensitive and deserves its own focused change + parity run.

A secondary, broader lever (already noted in `capture.py` as backlog P1.0): the 300m buffer over-feeds
**every** windowed group; per-group buffer-depth slicing before `compute_latest` would cut the recompute
tax across the board, not just momentum_run.

## What this PR ships

- **`quantlib/features/profile_sim.py`** — the pre-flight profiler: end-to-end bar→vector p50/p95/p99
  (slowest-shard-per-minute, last-bar anchor, write excluded) + a per-GROUP ranking. `make fp-profile-sim`.
- **`stream_sim.py`** — two **default-off** profiler hooks so the tool can attribute cost without changing
  the measured hot path:
  - `FP_SIM_GROUP_TIMINGS=1` → per-group `compute_latest` ms in the bench log (the per-feature/group
    attribution the phase decomposition could not give).
  - a cross-process `dispatch_wall` (reader) / `ready_wall` (worker) `time.time()` stamp pair → the true
    end-to-end bar→vector latency (the same last-bar anchor as `real_capture.feature_assemble_seconds`).
- **`docs/PROFILE_SIM.md`** — how to read the output; **`tests/test_fp_profile_sim.py`** — unit tests for
  the pure aggregators.

All existing tests (`test_fp_stream_sim`, `test_fp_latest`, …) pass unchanged; with the flags unset the
sim path is byte-for-byte what it was.

## What to monitor at the open

- Grafana `feature_assemble_seconds` (last-bar / pure-compute anchor) and `feature_vector_latency_seconds`
  (first-bar anchor, includes Alpaca delivery spread) per shard — the live analogue of this tool's
  end-to-end number.
- `feature_group_compute_seconds` per group — **watch `momentum_run` and `residual_analysis`**; they are
  the live drivers of the per-minute budget until the `compute_latest` override lands.
- `latency_slow_symbols` (TimescaleDB) — which tickers ran hottest when a shard spiked.

## Caveats on the numbers

- The sim **floods** (no inter-minute sleep), so the end-to-end number is a **stress ceiling** dominated by
  cross-process queue contention when all shards compute at once — it is intentionally pessimistic vs the
  real one-bar-per-minute cadence, and it is the honest worst case for "can we keep up".
- The per-shard *compute* decomposition (`stream_sim._report`) and the per-group ranking are
  contention-light at small shard counts but grow super-linearly with symbols/shard, so always profile at
  the shard size you intend to deploy.

## Re-measure 2026-06-18 (682 features / 51 groups) — regression check + current baseline

Run: `make fp-profile-sim N=1000 SHARDS=16 MIN=20` at the deployed commit (fingerprint
`0x710bed9e980616f3`). Same scale as the 06-15 reading above, so the two are directly comparable.

**End-to-end bar-arrival → universe-vector-ready (slowest shard / minute, write excluded):**

| date | universe / shards | features | end-to-end p50 | p95 | p99 |
|---|---|---|---|---|---|
| 2026-06-15 | 1000 / 16 | 519 | — | — | **~603ms** |
| 2026-06-18 | 1000 / 16 | 682 | 401ms | 616ms | **761ms** |

The +26% p99 (603→761ms) tracks the **+31% feature growth** (519→682, the order-flow batches PRs #115 et
al.) almost exactly — it is expected scaling, **NOT a regression**. The two dominant groups are stable
against their 06-15 readings: `momentum_run` p50 94ms (was ~95ms), `residual_analysis` p50 50ms (was the
top of its ~13–50ms range). Still **FAIL** vs the 100ms budget (7.6×), as designed — latency is on a
team-lead STAND-DOWN (`docs/LATENCY_PLAN.md` §7); this is the production-readiness number, not edge-blocking.

**Per-group `compute_latest` p50/p99 baseline (slowest shard each minute, post-warmup).** Diff the next
sim against this to catch a real per-group regression instead of eyeballing prose. Only the non-trivial
groups are listed; the long tail of reduction groups sits at the floor (~2.5ms p50 / ~4.5ms p99 each).

| group | p50 | p99 | note |
|---|---|---|---|
| `momentum_run` | 94ms | 269ms | dominant — Lever #2 target (`compute_latest` override / kernel migration) |
| `residual_analysis` | 50ms | 110ms | second — Lever #2 first move (→ ReductionGroup) |
| `daily_beta` | 20ms | 133ms | cross-sectional gather |
| `overnight_beta` | 12ms | 30ms | |
| `return_dispersion` | 12ms | 48ms | cross-sectional gather |
| `market_context` | 11ms | 113ms | cross-sectional gather |
| `gap_fill_state` | 11ms | 35ms | |
| `dumper_state` | 11ms | 57ms | |
| `runner_state` | 10ms | 72ms | |
| `liquidity_rank` | 9ms | 33ms | cross-sectional gather |
| reduction tier (33 groups) | ~2.5ms | ~4.5ms | each — already on the proven fast shape |

`SUM of per-group p50 = 333ms` (the per-minute serial compute budget; the end-to-end p99 is higher because
it is the slowest-shard tail, not the sum). The ranking is unchanged in SHAPE from 06-15: `momentum_run` +
`residual_analysis` together (~144ms p50) are ~43% of the per-minute compute — exactly the Lever #2 thesis
in `docs/LATENCY_PLAN.md` §7. No single group regressed; nothing to fix this cycle.
