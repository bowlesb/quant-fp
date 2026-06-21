# Feature latency expectations — the per-group production front door

> **What this is.** The single human-readable answer to "what does each feature group cost in production,
> why, and should it be on running/incremental state?" Per group: its KIND (Ben's A/B/Rust framing), its
> measured per-bet latency contribution, the mechanism that achieves it, and its incremental-readiness.
>
> **Canonical front door.** This consolidates the scattered machine-readable sources into one readable page:
> - [`docs/latency_budget.yaml`](latency_budget.yaml) — the per-group `compute_latest` ms **budget gate**
>   (`tests/test_fp_latency_budget.py`); the measured ms in the table below come from there.
> - [`docs/latency_e2e_budget.yaml`](latency_e2e_budget.yaml) — the **end-to-end** bar→vector ceiling gate
>   (`tests/test_fp_latency_e2e.py`, the #315 regression gate).
> - [`docs/INCREMENTAL_READINESS.md`](INCREMENTAL_READINESS.md) — the deeper kind/state/lever detail + the
>   PARKED corr-denom-straddle write-up. This doc is the readable summary; that one is the working detail.

## The end-to-end picture (read this first)

The number that matters for trading is **per-bet bar→vector**: from a minute's last bar arriving to THAT
bet's feature vector being ready. Measured (the all-work-merged 728-feature set, fp `0x873f2fceb8f00c92`):

| metric | now | target |
|---|---|---|
| **single-bet ISOLATED** (one 462-sym shard, no contention) | **~289ms p50** | <100ms |
| **typical bet UNDER LOAD** (16-shard universe, contended box) | **~935ms p50** | <100ms |
| universe-wide slowest-shard e2e (7400/16) | ~1.2–1.4s p50 | — |

**Two honest facts about the gap:**
1. The **~289ms isolated** floor is per-group COMPUTE. The per-bet vein of cheap structural wins (caching the
   static groups, sharing passes, latest-only folds) is largely harvested — only a **Rust-resident emit
   kernel** (folding all the per-group polars frame-builds into one resident pass) moves the 289ms toward
   <100ms. That's a coordinated fp-dev image build, not a quick win.
2. The **289ms → 935ms** gap under load is **CPU contention, NOT IPC**. Measured: the reader→shard transit is
   backpressure-blocking on saturated workers, not serialization — an Arrow/zero-copy transport claws back
   ~0ms. The gap is 16 shard processes + the live containers oversubscribing 32 cores. Levers there are
   ops/core-headroom + reducing what each shard computes (the FP_INCREMENTAL flip below), not a transport
   change.

**The two REAL levers (both Lead/Ben-sequenced):**
- **FP_INCREMENTAL enablement** of the 20 ready reduction groups — flips them from re-running the rolling
  recompute each minute to reading the pre-folded running sums (O(1)). The actual live latency payoff; gated
  on a careful PARITY=1 soak → PARITY=0. **20 of 23 reductions are parity-green and ready.**
- **Rust-resident emit kernel** — the only thing that moves the isolated 289ms floor.

**How it's enforced:** `tests/test_fp_latency_e2e.py` (#315) drives the REAL streaming path at a bounded
reference scale and asserts e2e p50/p99 stay under generous current-reality ceilings (p50 320ms / p99 420ms
— a regression floor, NOT the 100ms target). The per-group budgets (`test_fp_latency_budget.py`) catch a
single group regressing in isolation.

## The KINDS (Ben's framing — "should it be on running state?")

| kind | meaning | per-minute cost | "on running state?" |
|---|---|---|---|
| **A — intraday-invariant** | output is a pure function of a per-session-constant snapshot (daily / reference); compute ONCE per day, cache, broadcast | ~0 | YES — already cached |
| **B — incremental sum** | windowed reduction; the running per-(symbol,window) Σ is folded O(1)/minute (`WindowedSumState`), backfill is the parity oracle | O(symbols×windows) | YES — running sums (gated on FP_INCREMENTAL flip) |
| **B — latest-only fold** | session-cumulative or window-anchored; reduced to a single per-(symbol,session) aggregate at T (no per-minute scan) | small | partially — could promote to a declared CumulativeState kind |
| **Rust-resident** | sequential-hot per-symbol fold already in a Rust kernel (EMA/lag/extrema/swing) | O(1)/minute in-kernel | YES — done |
| **Gather** | universe cross-sectional reduce; runs ONCE in the reader phase, ~7ms, NOT a per-bet cost | n/a (reader-phase) | n/a |
| **hand-written** | bespoke `compute_latest`; a candidate to migrate to a kind or Rust kernel | varies | candidate |

## Per-group table

**63 groups / 728 features.** `ms` = the measured single-shard `compute_latest` cost at the reference shard
(312 tickers, from `latency_budget.yaml`). NOTE: this is the per-group profiling view — it OVER-counts the B
(incremental-sum) groups (they share ONE batched incremental emit in flow, so price_volume's standalone
~110ms is not its in-flow share) and excludes the reader gather/IPC; the e2e gate is the honest bar→vector
number. Use these for RELATIVE ranking + regression detection. Sorted by cost.

| group | feat | ms | KIND | mechanism today | incremental-ready |
|---|---|---|---|---|---|
| `price_volume` | 70 | 109.8 | B incremental-sum | shared running-sum (WindowedSumState) | **PARKED** (corr-denom) |
| `distribution` | 20 | 75.4 | B incremental-sum | shared running-sum | **READY** |
| `volume_leads_price` | 12 | 72.8 | B incremental-sum | shared running-sum | **READY** |
| `liquidity` | 15 | 69.3 | B incremental-sum | shared running-sum | **READY** |
| `return_dynamics` | 15 | 55.4 | B incremental-sum | shared running-sum | **READY** |
| `subminute_gap_fano` | 1 | 52.0 | hand-written | bespoke compute_latest (per-minute tick group-by) | Rust-kernel candidate |
| `momentum_consistency` | 18 | 48.9 | B incremental-sum | shared running-sum | **READY** |
| `price_returns` | 40 | 44.3 | Rust-resident | StatefulEngine (EMA/lag/extrema fold) | done — resident |
| `clean_momentum` | 12 | 36.7 | B incremental-sum | shared running-sum | **READY** |
| `market_beta` | 21 | 32.8 | B incremental-sum | shared running-sum | **PARKED** (corr-denom) |
| `momentum_run` | 12 | 32.0 | B latest-only | own latest-only (skew+streak) | done — assessed irreducible |
| `market_turbulence` | 5 | 29.3 | Gather | universe gather (reader-phase) | n/a — gather |
| `size_entropy` | 2 | 28.4 | hand-written | bespoke compute_latest | Rust-kernel candidate |
| `breadth` | 30 | 25.3 | Gather | universe gather (reader-phase) | n/a — gather |
| `volume_exhaustion` | 10 | 23.3 | B incremental-sum | shared running-sum | **READY** |
| `trend_quality` | 30 | 22.6 | B incremental-sum | shared running-sum | **READY** |
| `swing` | 9 | 21.9 | Rust-resident | quant_tick.swing_fold Rust kernel | done — resident |
| `technical` | 14 | 19.2 | Rust-resident | StatefulEngine (EMA fold) | done — resident |
| `momentum` | 22 | 19.0 | B incremental-sum | shared running-sum | **READY** |
| `quote_spread` | 21 | 18.9 | B incremental-sum | shared running-sum | **READY** |
| `ohlc_vol` | 12 | 17.1 | B incremental-sum | shared running-sum | **READY** |
| `sector_return` | 8 | 17.0 | Gather | universe gather (reader-phase) | n/a — gather |
| `sector_beta` | 6 | 16.9 | Gather | universe gather (reader-phase) | n/a — gather |
| `residual_analysis` | 6 | 16.5 | B incremental-sum | shared running-sum | **PARKED** (SSR perfect-fit) |
| `volatility` | 15 | 16.5 | B incremental-sum | shared running-sum | **READY** |
| `trade_flow` | 23 | 15.7 | B incremental-sum | shared running-sum | **READY** |
| `price_levels` | 21 | 13.9 | Rust-resident | StatefulEngine (ExtremaState fold) | done — resident |
| `signed_trade_ratio` | 4 | 13.8 | B incremental-sum | shared running-sum | **READY** |
| `print_hhi` | 2 | 13.5 | hand-written | bespoke compute_latest | Rust-kernel candidate |
| `return_dispersion` | 10 | 13.0 | A cached/static | SessionCache daily memo (#281) | n/a — A cached |
| `efficiency` | 18 | 12.8 | B incremental-sum | shared running-sum | **READY** |
| `cross_sectional_rank` | 6 | 12.0 | Gather | universe gather (reader-phase) | n/a — gather |
| `volume` | 23 | 11.9 | B incremental-sum | shared running-sum + centered-std (#307) | **READY** |
| `range_expansion` | 2 | 10.7 | B incremental-sum | shared running-sum | **READY** |
| `trade_freq_z` | 4 | 10.1 | B incremental-sum | shared running-sum | **READY** |
| `microstructure_burst` | 4 | 9.6 | hand-written | bespoke compute_latest | candidate |
| `candlestick` | 12 | 9.4 | Rust-resident | StatefulEngine (LastKState fold) | done — resident |
| `realized_range` | 3 | 9.1 | B incremental-sum | shared running-sum | **READY** |
| `inter_arrival` | 3 | 9.0 | hand-written | bespoke compute_latest | candidate |
| `count_fano` | 1 | 8.5 | B incremental-sum | shared running-sum | **READY** |
| `market_context` | 36 | 8.2 | Gather | universe gather (reader-phase) | n/a — gather |
| `intraday_seasonality` | 2 | 8.0 | B latest-only | own latest-only session agg (#286) | done — latest-only |
| `calendar_events` | 7 | 7.6 | A cached/static | consolidated point-in-time pass | n/a — A cached |
| `runner_state` | 6 | 6.0 | B latest-only | shared session-cumulative pass (#285) | done — latest-only |
| `gap_fill_state` | 2 | 5.3 | B latest-only | shared session-cumulative pass (#285) | done — latest-only |
| `trade_size_dist` | 3 | 4.9 | hand-written | bespoke compute_latest | candidate |
| `calendar` | 4 | 4.8 | A cached/static | consolidated point-in-time pass | n/a — A cached |
| `draw_range` | 3 | 4.6 | B latest-only | own latest-only window agg (#257) | done — latest-only |
| `large_print_burst` | 3 | 4.6 | hand-written | bespoke compute_latest | candidate |
| `dumper_state` | 6 | 4.5 | B latest-only | shared session-cumulative pass (#285) | done — latest-only |
| `tick_runlength` | 3 | 3.9 | hand-written | bespoke compute_latest | candidate |
| `prior_day` | 10 | 2.7 | A cached/static | consolidated daily-broadcast pass | n/a — A cached |
| `sector` | 12 | 2.6 | A cached/static | consolidated point-in-time pass | n/a — A cached |
| `edgar_filing_frequency` | 10 | 1.9 | A-hybrid (event-kind) | SessionCache filings; intraday available_at<=minute gate | n/a — event-kind |
| `liquidity_rank` | 2 | 1.9 | A cached/static | SessionCache daily memo (#281) | n/a — A cached |
| `multi_day_returns` | 28 | 1.6 | A cached/static | consolidated daily-broadcast pass | n/a — A cached |
| `multi_day_vwap` | 10 | 1.5 | A cached/static | consolidated daily-broadcast pass | n/a — A cached |
| `daily_beta` | 3 | 1.4 | A cached/static | SessionCache daily memo (#281) | n/a — A cached |
| `overnight_beta` | 3 | 1.4 | A cached/static | SessionCache daily memo (#262) | n/a — A cached |
| `overnight_intraday_split` | 3 | 1.3 | A cached/static | SessionCache daily memo (#281) | n/a — A cached |
| `asset_flags` | 4 | 0.9 | A cached/static | consolidated point-in-time pass | n/a — A cached |
| `round_levels` | 3 | 0.8 | A cached/static | consolidated point-in-time pass | n/a — A cached |
| `peer_relative` | 3 | — | Gather | universe gather (reader-phase) | n/a — gather |

## What the table reveals (the at-a-glance summary)

**Kind breakdown of the 63 groups:**
- **23 B — incremental-sum** (the windowed reductions / `WindowedSumState`): the ones that should ride the
  running sums. **20 are parity-green and ready** for the FP_INCREMENTAL flip; **3 are PARKED** (price_volume,
  market_beta, residual_analysis — the corr-denom-straddle / perfect-fit-SSR class, a genuinely harder
  cancellation problem the centering abstraction can't reach — see INCREMENTAL_READINESS.md §Parked).
- **14 A — intraday-invariant** (cached static / daily-broadcast): already compute-once-per-day, ~0/minute.
- **14 B — latest-only fold** (session-cumulative + window-anchored): already reduced to a single per-session
  aggregate at T; a subset are clean candidates to promote to a declared CumulativeState kind.
- **5 Rust-resident** (price_returns, technical, price_levels, candlestick, swing): already in-kernel.
- **7 Gather** (market_context, breadth, sector_beta, …): run ONCE in the reader phase (~7ms total), NOT a
  per-bet cost — they do not gate a single bet's latency.
- **~8 hand-written** candidates (the Layer-C tape groups — subminute_gap_fano 52ms is the heaviest; their
  cost is the per-minute tick group-by, which only a Rust kernel collapses — Rust-kernel candidates).

**The bottom line for Ben:** the groups that *should* be on running state already are (A cached, B
latest-only, Rust-resident), or are one parity-gated flip away (the 20 ready B-incremental-sum). What's left
to move the per-bet number is (1) the Lead's FP_INCREMENTAL flip of those 20 (the live payoff) and (2) the
Rust-resident emit kernel for the isolated-compute floor — both real, sequenced engine investments, not
scattered per-group work.
