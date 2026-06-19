# Lane C — SCOPE / HORIZON: cross-sectional CLOSE→NEXT-OPEN overnight directional edge

PRE-REGISTERED 2026-06-19 (BEFORE any result). HEAD SHA `5d280dd2a325d7e06c50f1f8e0cc6cfa5c0de9c4`
(branch `main`). Author: Modeller / Lane C. No threshold relaxation after seeing numbers.

## Motivation (why this leaves the settled null)

The intraday US-equity cross-sectional DIRECTION cut is a triple-confirmed NULL (price-only,
GPU-embeddings, order-flow CORE) on the trusted/parity-true substrate. The consistent diagnostic:
our features carry intensity / liquidity / volatility information, but **not intraday cross-sectional
DIRECTIONAL alpha** — exactly the regime where HFT arbitrage is most efficient.

Lane C leaves that cut. The **overnight (close → next-open) return** is a structurally distinct
regime: intraday HFT arbitrage does not operate while the market is closed, and slower
risk-premia / underreaction / overnight-drift effects are documented to persist there. The cheapest,
highest-signal first test reuses data we ALREADY have on disk: **18 months of minute bars**
(`/store/raw/bars/`, 2024-12-11 → 2026-06-18, 380 trading days, ~7600 symbols/day). That is a far
deeper and broader panel than the 42-day intraday clean window the nulls were measured on — so a
weak-but-real horizon edge is far better powered here, AND the holding regime is different. Both the
"different regime" and the "more power" arguments cut the same way.

NOTE ON FEATURE SUBSTRATE: the computed feature STORE is only backfilled 46 days (2026-04-15→06-18);
the deep history lives in the RAW BARS. To get the 18-month power, X is computed directly from the
raw DAILY bars (point-in-time, as-of the close), NOT read from the shallow feature store. The
predictors are deliberately simple, transparent, and slow (the documented overnight-anomaly family):
prior-day return, multi-day momentum, short-horizon reversal, realized vol, prior overnight return.
This is a SCOPE/HORIZON test of whether a DIRECTIONAL edge exists overnight — not a feature-richness
test. If the simple overnight panel shows signal, a follow-up enriches X from the feature store on the
(shallower) window; if it shows nothing on a deep, powered panel, the overnight directional cut joins
the intraday null with high confidence.

## The ONE falsifiable sub-test (single pre-registered hypothesis)

**H1:** A cross-sectional model trained on END-OF-DAY (as-of-the-close) predictors has out-of-sample
skill at ranking the next CLOSE→OPEN overnight excess return, beyond a within-day shuffle null, and
that skill survives realistic overnight execution costs.

### Label (y) — tradeable close→next-open overnight excess return

For each (symbol, trading day d):

- **Entry** = the day-d RTH **close** price = the `close` of the **15:59 ET (19:59 UTC)** minute bar.
  This is a fillable, highly-liquid price (market-on-close auction prints at 16:00 ET; the 15:59
  close is the conservative tradeable proxy and avoids any reliance on a single auction tick).
- **Exit** = the day-(d+1) **tradeable open** = the `close` of the **09:35 ET (13:35 UTC)** minute bar
  of the NEXT trading day. Per the [[reference-quant-tradeable-entry-trap]] discipline we DO NOT use
  the 09:30 open print (that is the gap-fade look-ahead trap — the open print is frequently not
  fillable at scale). The 09:35 close is a conservatively fillable exit and is the same 09:35-ET
  convention the intraday baseline already uses for entry.
- **Raw overnight return** = `exit_close / entry_close - 1`.
- **Cross-sectional excess** = `raw_overnight_return − (cross-sectional MEDIAN over all symbols
  enterable on day d)`, identical to the intraday builder's `cross_sectional_excess_frame`.
  Breadth floor `MIN_CROSS_SECTION` (same constant as the intraday panel). The "timestamp" key for
  the harness is the day-d close datetime (one cross-section per trading day).

This is a clean tradeable strategy: enter at the close (MOC), hold flat overnight, exit at the next
09:35. Non-overlapping daily labels → no overlapping-label inflation.

### Liquidity / universe filter (at the ENTRY minute, day d, point-in-time)

A symbol is enterable on day d only if:
- the 15:59-ET entry bar exists AND `close * volume >= $50,000` (same `MIN_DOLLAR_VOL` as intraday), AND
- the next-day 09:35-ET exit bar exists (so the label is realizable).

Cross-sectional median is taken over the enterable set on day d. No survivorship beyond "had bars on
both legs" — which is the honest tradeable universe.

### Features (X) — END-OF-DAY, computed point-in-time from RAW DAILY bars (NO look-ahead)

Per (symbol, day d), computed using ONLY bars at-or-before the day-d close (a daily bar = the RTH
session 09:30–16:00 ET aggregated: open=09:30 open, close=15:59 close, high/low/volume/vwap over RTH):

1. `ret_1d` — day-d RTH return: `close_d / open_d − 1` (intraday).
2. `ret_co_1d` — prior close→close: `close_d / close_{d-1} − 1`.
3. `ret_2d`, `ret_5d`, `ret_10d`, `ret_20d` — multi-day momentum: `close_d / close_{d-k} − 1`.
4. `overnight_prev` — the PRIOR overnight return `open_d / close_{d-1} − 1` (overnight autocorrelation /
   reversal is the canonical overnight predictor).
5. `intraday_prev` — prior-day intraday `close_{d-1} / open_{d-1} − 1` (intraday→overnight spillover).
6. `rvol_5d`, `rvol_20d` — realized vol = stdev of daily close-close returns over the trailing window.
7. `dollar_vol_20d` — log mean daily dollar volume over 20d (liquidity / size proxy).
8. `gap_z` — day-d close vs 20d mean / 20d std (a z-scored level / reversal proxy).
9. `range_20d_pos` — position of close_d in the trailing 20d [low,high] range (0..1).

All are slow, daily, point-in-time, and require ≥ 21 trailing daily bars (so the first ~20 days of
the panel are warmup-only and produce no labelled rows). NO feature uses day-(d+1) data.

### Harness / discipline (reuse `quantlib.research.run_experiment`)

- **Model:** the same disciplined LightGBM via `run_experiment` (walk-forward, NW-t, shuffle canary,
  net-of-cost L/S backtest). `label="raw"` headline; `label="rank"` reported as a robustness view.
- **Walk-forward purge:** `horizon_minutes=1440` (purge ≥ 1 calendar day between train and test so no
  training label peeks into the test block). `n_folds=5` expanding-window over the ~360 daily
  cross-sections (vastly more folds-worth of timestamps than the 42-day intraday panel).
- **Cadence / annualization:** `cadence_min=390` → `periods_per_year = 252·(390/390) = 252` (one
  rebalance per trading day, the correct overnight annualization). This yields NW `lag = max(1,
  1440//390) = 3` — overnight daily labels are NON-overlapping so the true lag is 1; lag=3 is a
  CONSERVATIVE (t-deflating) choice, never inflating significance.
- **Baselines (both):**
  - **predict-zero** — constant prediction has 0 within-cross-section rank-IC by construction.
  - **within-day SHUFFLE canary** — labels shuffled within each day's cross-section; the only
    legitimate null. Edge = REAL OOS IC − canary IC must be materially positive.
- **Costs:** overnight has WIDER effective spreads than intraday (MOC + next-open). The default 2bps
  one-way is too generous; the economic gate below uses the model's `breakeven_cost_bps` and requires
  it to clear a realistic overnight round-trip. The L/S backtest is dollar-neutral top/bottom-decile.

### FALSIFIABLE SUCCESS CRITERION (hard thresholds — fixed BEFORE results)

H1 is a **HIT** iff, at the 1-day overnight horizon, on the out-of-sample walk-forward folds, ALL of:

1. **REAL OOS mean rank-IC ≥ 0.01**, AND
2. **REAL OOS IC − SHUFFLE canary IC ≥ 0.01** (skill beyond the within-day shuffle null), AND
3. **Newey–West |t| ≥ 2.0** on the per-day OOS IC series (lag=3 as above), AND
4. **net-of-cost L/S is economically viable:** `breakeven_cost_bps > 10.0` — i.e. the gross signal can
   absorb at least a 10 bps one-way cost before net ≤ 0. Justification for the 10 bps bar: overnight
   round-trip = a market-on-close fill (~1–3 bps for liquid names, higher in the tail) PLUS a next-open
   fill (open auctions / first-5-min spreads are materially wider than mid-session, commonly 5–15 bps
   for the broad universe). A breakeven < 10 bps one-way (< ~20 bps round-trip) would NOT survive a
   realistic overnight cross-section of 7600 names skewed to small/illiquid; 10 bps is the honest
   minimum a tradeable overnight L/S must clear. (This is STRICTER than the intraday 2bps default,
   deliberately, because overnight costs are worse.)

Anything short of ALL FOUR is a **MISS / honest null** for the overnight directional cut, reported
with the by-horizon table (1-day headline; 2-day and 3-day forward holds reported as the
secondary by-horizon view — same gate, multi-day exit = day-(d+k) 09:35 close).

### Multiplicity note

The headline is ONE pre-registered test (1-day overnight). The 2-day / 3-day forward holds are
reported as a by-horizon DESCRIPTIVE table, NOT as additional shots at the headline. If ANY multi-day
horizon is later promoted to a claim, a BY-FDR family correction (q=0.10, the Lane-standard, valid
under arbitrary dependence) over {1d, 2d, 3d} will be applied BEFORE that claim — no max-over-horizon
disjunction without the family correction (the exact trap flagged for the order-flow verdict).

## Plan / compute discipline

- **Build (polars):** `build_overnight_dataset.py` in an fp-dev container (`feature-computer`),
  memory-bounded one date at a time, reading raw daily bars. Writes
  `experiments/data/overnight_panel.parquet` + per-horizon npz `overnight_panel_fwd_{1,2,3}d.npz`.
- **Run (lightgbm):** `run_baseline.py` (the sibling reader, NPZ_PREFIX=overnight_panel) in a
  `quant-experimenter:latest` throwaway, OMP/LGBM threads capped to 2 so it does NOT starve live
  capture or Lane A.
- **COMPUTE-BOUNDED:** load avg ~8 on a 32-core box at claim time (other lanes running). If load stays
  high, run a SMALL smoke (a ~20-day slice or top-1000-by-dollar-vol universe) to validate the
  pipeline end-to-end and defer the full 18-month walk-forward; else run the full panel.
- Research scratch ONLY in `experiments/`. NO quantlib edit, NO live-tree edit, NO fingerprint/deploy,
  no secrets printed.

## Stamped data state

- Raw bars: `/store/raw/bars/` (fp_store_real), 2024-12-11 → 2026-06-18, 380 trading days,
  ~7600 symbols/day (verified 2026-06-19).
- HEAD `5d280dd2a325d7e06c50f1f8e0cc6cfa5c0de9c4` @ branch `main`.
- Reused harness: `quantlib.research.run_experiment`, `quantlib.backtest.*` (UNMODIFIED).
