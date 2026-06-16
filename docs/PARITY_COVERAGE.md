# Feature Parity Coverage — live (`compute_latest`) == backfill (`compute`)

The platform's core invariant: for EVERY registered feature, the LIVE code path (the aggregate-at-T
`compute_latest` / the per-minute `IncrementalEngine`) must emit, for the latest minute, EXACTLY what the
BACKFILL path (`compute`, whole-history rolling) computes for that minute — within each feature's declared
tolerance. This doc is the living source of truth for that invariant: every group, how its live path works,
and its parity status as last audited on REAL `/store/raw` data.

**Re-run the audit** (the authoritative check, on production-realistic data from `/store/raw`):

```bash
# in a memory-bounded fp-dev sandbox with the real store mounted read-only at /store
MEM=12g CPUS=8 ops/sandbox.sh "python -m quantlib.features.parity_audit 2026-06-15"
```

It loads real bars + real per-minute tick aggregates (from raw trades/quotes) + a resampled daily history
into the SAME `BatchContext` shape the live capture uses, runs `compute()` vs `compute_latest()` (and, for
reduction groups, the `IncrementalEngine.step` minute-by-minute path — the actual production live path) for
every runnable group, and compares the latest minute cell-for-cell within each feature's tolerance. Exit
code is non-zero on any divergence. The pytest guards (`tests/test_fp_latest.py`,
`tests/test_fp_incremental_features.py`, `tests/test_fp_momentum_run.py`) hold the same invariant on
synthetic/golden frames in CI; the real-data audit is the stronger periodic check.

## Last audit

- **Day:** 2026-06-15 (settled), **96 liquid multi-sector symbols** (SPY/QQQ/IWM/DIA + ~92 names across every
  GICS sector — broadened from the original 14 so the cross-sectional rank / breadth groups rank over a REAL
  distribution), real bars+trades+quotes from `/store/raw`, ~960-minute buffer, ~126 days of resampled daily
  history. Re-run any size with `FP_AUDIT_SYMBOLS=...` or a positional count.
- **`compute_latest` vs `compute().last`:** 606 features → **589 MATCH, 7 DIVERGE, 10 NEEDS_DATA** — where ALL
  7 DIVERGE are on a SINGLE barely-trading symbol (**UBER**, ~3 bars / 2 distinct closes in the last window —
  a numerically-degenerate OLS fit, see below). Excluding UBER: **596 MATCH / 0 DIVERGE.**
- **`IncrementalEngine.step` (minute-by-minute) vs `compute().last`:** 335 reduction features →
  **327 MATCH, 8 DIVERGE** — 7 on UBER (same degenerate fits, the incremental running-sum drift amplifying
  them) + 1 on DIS (`idio_vol_90m`, a ~1.6e-8 near-zero idiosyncratic vol). Excluding UBER: **334 MATCH /
  1 DIVERGE** (the DIS near-zero).
- The 10 NEEDS_DATA are deep multi-day windows (`daily_return_180d/240d`, `dist_from_250d_high`,
  `nasdaq_return_120m`, `market_return_10m/20m` at the very-last-minute lag) that are all-null at the latest
  minute in BOTH paths because the store has only ~126 days of history — a legitimate warmup-null, not a
  divergence (verified: the all-null column set is IDENTICAL live and backfill).

**Broadening (unit 1) confirmed the cross-sectional/breadth verdict** beyond the original modest 14-name
breadth: with 96 symbols spanning all sectors, `cross_sectional_rank`, `breadth`, `market_context`, and
`market_beta` all MATCH on every healthy symbol. It also surfaced + fixed two real sparse-symbol bugs (NaN
clip + window-edge return) the dense set never exposed — see below.

## Live-path taxonomy

Every group's `compute_latest` is one of four kinds. The first three are parity-true **by construction**;
only `custom-override` carries hand-written aggregate-at-T logic that could silently diverge — those are the
audit's primary scrutiny.

| Path | How the live form is derived | Parity argument | # groups |
|------|------------------------------|-----------------|----------|
| `base(recompute->filter)` | default `FeatureGroup.compute_latest` = `compute()` filtered to the last minute | identical code; trivially equal | 8 |
| `window-sliced` | `compute_latest_on_window`: the SAME `compute()` on the buffer sliced to the group's trailing window | same rolling code on the minimal input window | 2 |
| `reduction(generated)` | `ReductionGroup` generates BOTH `compute` (polars rolling) and `compute_latest` (Rust aggregate-at-T) from ONE declaration; plus the `IncrementalEngine` running-sum path | both forms materialise the SAME canonical aggregate columns and evaluate the SAME `assemble()` exprs | 15 |
| `custom-override` | hand-written aggregate-at-T `compute_latest` | held to byte-equality by the parity tests + this audit | 10 |

## Per-group status

Status legend: **MATCH** = verified equal on real data within tolerance; **CONSTRUCTION** = parity-true by
construction (same code path), verified MATCH; **WARMUP** = some deep-window features all-null at T in both
paths (data-depth, not a divergence).

| Group | Type | N | Live path | Status | Notes |
|-------|------|---|-----------|--------|-------|
| asset_flags | reference | 4 | base | CONSTRUCTION/MATCH | static reference; pointwise |
| calendar | calendar | 4 | base | CONSTRUCTION/MATCH | pointwise calendar |
| calendar_events | calendar | 7 | base | CONSTRUCTION/MATCH | pointwise calendar |
| microstructure_burst | microstructure | 4 | base | CONSTRUCTION/MATCH | raw `trades` input; per-minute |
| round_levels | price | 3 | base | CONSTRUCTION/MATCH | pointwise price |
| sector | reference | 12 | base | CONSTRUCTION/MATCH | static reference one-hot |
| swing | trend_quality | 9 | base | CONSTRUCTION/MATCH | recompute→filter |
| tick_runlength | microstructure | 3 | base | CONSTRUCTION/MATCH | raw `trades` input; per-minute |
| breadth | cross_sectional | 30 | custom-override | MATCH | universe-wide reduce (gather phase) |
| candlestick | candlestick | 12 | custom-override | MATCH | |
| cross_sectional_rank | cross_sectional | 6 | custom-override | MATCH | ranks over the pinned `universe` set |
| market_context | cross_sectional | 36 | custom-override | MATCH (WARMUP) | market_return_10m/20m, nasdaq_120m null@T (both paths) |
| multi_day_returns | multi_day | 28 | custom-override | MATCH (WARMUP) | 25/28 warm with 125d bar history; only 180d/240d/250d still NEEDS_DATA (need >125 trading days) |
| multi_day_vwap | multi_day | 10 | custom-override | MATCH | now FULLY warm (10/10) with 125d bar history |
| price_levels | price | 21 | custom-override | MATCH | |
| price_returns | price | 40 | custom-override | MATCH | |
| prior_day | multi_day | 10 | custom-override | MATCH | |
| technical | technical | 14 | custom-override | MATCH | |
| clean_momentum | trend_quality | 12 | reduction | CONSTRUCTION/MATCH | incremental-verified (UBER degenerate-fit residual, see KNOWN LIMITATION) |
| distribution | volatility | 20 | reduction | CONSTRUCTION/MATCH | upside/downside_vol sqrt-clip fixed (sparse-symbol NaN) |
| efficiency | momentum | 18 | reduction | CONSTRUCTION/MATCH | lag-point group; incremental-verified |
| liquidity | trade_flow | 15 | reduction | CONSTRUCTION/MATCH | tick-column input; incremental-verified |
| market_beta | cross_sectional | 21 | reduction | CONSTRUCTION/MATCH | broadcast regressor; incremental-verified |
| momentum | momentum | 22 | reduction | CONSTRUCTION/MATCH | incremental-verified |
| momentum_consistency | momentum | 18 | reduction | CONSTRUCTION/MATCH | lag-point group; incremental-verified |
| ohlc_vol | volatility | 12 | reduction | CONSTRUCTION/MATCH | incremental-verified |
| price_volume | price_volume | 70 | reduction | CONSTRUCTION/MATCH | OBV cumulative regressor; incremental-verified |
| quote_spread | quote_spread | 21 | reduction | CONSTRUCTION/MATCH | tick-column input; incremental-verified |
| return_dynamics | momentum | 15 | reduction | CONSTRUCTION/MATCH | lag-point group; incremental-verified |
| trade_flow | trade_flow | 23 | reduction | CONSTRUCTION/MATCH | tick-column input; incremental-verified |
| trend_quality | trend_quality | 30 | reduction | CONSTRUCTION/MATCH | time-axis regressor; incremental-verified |
| volatility | volatility | 15 | reduction | CONSTRUCTION/MATCH | parkinson_vol sqrt-clip fixed (sparse-symbol NaN); incremental-verified |
| volume | volume | 23 | reduction | CONSTRUCTION/MATCH | incremental-verified |
| momentum_run | trend_quality | 12 | window-sliced | MATCH | residual_skew tolerance fixed (see below) |
| residual_analysis | trend_quality | 6 | window-sliced | MATCH | |

**Totals:** 35 groups, 606 features. 8 base, 2 window-sliced, 15 reduction, 10 custom-override.

## Divergences found & fixed

### `momentum_run.residual_skew_{5,10,15,20,30,60}m` — tolerance too tight for a third moment (FIXED)

`residual_skew` is a THIRD moment `m3 / m2**1.5` built from centered third-order power sums of the
close-vs-time fit — each sum a catastrophic-cancellation difference (`sxxx_c = sxxx - 3·mx·sxx + 3·mx²·sx -
n·mx³`). The whole-buffer rolling `compute()` and the window-sliced `compute_latest` sum those huge
pre-cubed columns over **different array lengths**, so they round a near-zero skew differently. On real
`/store` data this produced a bounded gap (max |Δ| ~5e-4, blowing up to ~1.8e-2 RELATIVE only where the skew
itself is ~0) that exceeded the group's tight `RUN_TOL=1e-4` on 6 features.

The window-sliced live form is the better-conditioned one; the divergence is pure float noise, not a logic
error. **Fix:** give `residual_skew` its own `SKEW_TOL = 0.02` (the same tolerance `realized_vol` — a
second-moment — already declares for the analogous cancellation), keeping `longest_streak` (an exact run
length) at the tight `RUN_TOL`. Guarded by `tests/test_fp_momentum_run.py::
test_residual_skew_window_sliced_latest_matches_rolling_on_deep_buffer`, which reproduces the cancellation
on tick-quantized prices and asserts the gap exceeds the old tolerance but stays within the new one.

### `volatility.parkinson_vol_*` + `distribution.upside_vol_*`/`downside_vol_*` — sqrt of a negative running-sum residue on sparse symbols (FIXED)

Found by the **96-symbol** broadening (the original 14 mega-caps were too dense to trigger it). Each is a
`sqrt` of a mathematically NON-NEGATIVE windowed mean/sum (`mean(log(high/low)²)`, `mean(squared returns)`).
On a SPARSE symbol whose in-window bars are all flat (`high==low` → `hl2==0`) or all one-signed, the live
`IncrementalEngine` running sum drifts to a tiny NEGATIVE residue (~−1e−22) from the add/expire float cycle,
so `sqrt(negative) = NaN` — while the backfill `rolling_mean`/`rolling_sum` returns exactly `0.0`. A
null/NaN-vs-value parity break on DIS/C/VZ. **Fix:** clip the non-negative quantity to `>= 0` before the
`sqrt` (`.clip(lower_bound=0.0).sqrt()`) — exactly what `ohlc_vol`'s garman_klass/rogers_satchell and
`clean_momentum` already do for the identical drift; these two groups simply missed it. 6 incremental
divergences → 0.

### `momentum_run.longest_streak_*` — window-edge return nulled on a sparse symbol (FIXED)

Found by the broadening on **DIS** (2 bars 47 min apart in the trailing window): `longest_streak_60m` was
`0.033` in backfill but `null` live. `longest_streak` reads a per-bar `close.shift(1)` return at every
in-window bar; the EARLIEST in-window bar's POSITIONAL predecessor sits an arbitrary gap before the window,
and the blunt `compute_latest_on_window` minute-cutoff slice DROPPED it — nulling the boundary return live
while the whole-buffer backfill resolved it. The slice can't simply keep the prior bar for the WHOLE group,
because `residual_skew` (float-order-sensitive third sums) wobbles if it sees any bar beyond its window.
**Fix:** `momentum_run.compute_latest` now runs the two features on the slices each needs — `residual_skew`
on the tight `LOOKBACK_MINUTES` window, `longest_streak` on that window PLUS each symbol's one prior bar
(`_slice_with_prior_bar`) — and stitches them at T. Both halves run the identical `compute()` math on their
minimal input, so live == backfill for dense AND sparse symbols. Guarded by `tests/test_fp_momentum_run.py`.

### KNOWN LIMITATION — numerically-degenerate OLS / near-zero values on barely-trading symbols (UBER, DIS)

The residual divergences after the fixes above are ALL on near-degenerate inputs where the feature value is
itself numerically meaningless, NOT a platform logic error:

- **UBER** `trend_quality.price_r2_{10,15,20}m`, `clean_momentum.clean_momentum_score_*`,
  `technical.bb_position_20m`, `trend_strength_15m`: UBER's trailing windows held **3 bars with 2 distinct
  closes** (72.99, 72.99, 72.9899) — a near-singular OLS fit. `price_r2 ≈ 0.987` has an essentially-zero
  denominator, so the single-pass Rust kernel (`compute_latest`, stable ~3.6e-4 off) and the rolling polars
  form (`compute`) round it differently; the incremental running sums (`step`) drift further (up to ~6.5e-3,
  GROWING with warmup depth — the documented running-sum drift, bounded in production by the daily re-seed).
- **DIS** `market_beta.idio_vol_90m`: a ~1.6e-8 idiosyncratic vol (i.e. ~0); the relative-tolerance gauge is
  meaningless at that magnitude.

These are genuinely path-different-by-design at the numerical-degeneracy boundary: a relative tolerance
can't gauge a near-zero or near-singular quantity, and tightening the per-feature degeneracy guards to null
them risks over-nulling healthy data — a per-feature judgement deferred rather than rushed during live hours.
They affect only symbols barely trading in extended hours and are honestly reported (not silently passed).
The audit flags them every run, so if they ever broaden beyond such degenerate inputs we will see it.

## Method notes / honesty

- The audit feeds REAL tick columns (n_trades, signed_volume, spread, imbalance, sizes) aggregated from
  `/store/raw/trades`+`/store/raw/quotes` so trade_flow / quote_spread / liquidity run on production-shaped
  inputs (not synthetic zeros). The aggregator in the harness reproduces the production tick columns; it is
  not a re-certification of `quantlib.aggregates` (which has its own tests) — its purpose is to exercise the
  parity invariant on varied real values.
- The `IncrementalEngine` MUST be driven minute-by-minute (seed, then `step` each new minute), exactly as
  the live capture drives it — a single `step` on a cold full buffer mis-seeds the stateful regressors (OBV
  cumulative, time-axis origin) and is NOT a production scenario. The harness steps the trailing
  `WARMUP_MINUTES` (260) one at a time before comparing.
- A feature that is all-null at T in BOTH paths is reported **NEEDS_DATA** (warmup / insufficient history),
  never silently counted as MATCH. A feature non-null in backfill but null live (or beyond tolerance) is a
  hard **DIVERGE**.
- Groups requiring inputs absent from the audit frames are reported NEEDS_DATA ("inputs not present"), not
  assumed matching.
