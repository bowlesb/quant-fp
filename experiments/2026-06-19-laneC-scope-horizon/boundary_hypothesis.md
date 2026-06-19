# Lane C — NET-OF-COST LIQUIDITY-BOUNDARY ADJUDICATION (neutral)

STAMPED **2026-06-19**, BEFORE any net-of-cost band result. Adjudicator: Lead/OvernightAdj
(NEUTRAL, not an edge-hunter). HEAD `5d280dd2a325d7e06c50f1f8e0cc6cfa5c0de9c4` @ `main`.
Reused harness: `quantlib.research.run_experiment` + `quantlib.backtest.long_short_backtest`,
BOTH UNMODIFIED. Cache: `experiments/data/overnight_daily_full.parquet` (stage-1 daily table,
380 days 2024-12-11..2026-06-18, 7383 symbols). NO minute re-scan, NO quantlib edit, NO deploy.

## The question (single, faithful, no-tuning)

Lane C established a REAL full-universe overnight close→next-open directional signal
(1d: IC 0.0354, NW t=3.89, breakeven 21.95 bps, shuffle-clean) that COLLAPSES on the
top-1500-by-ADV liquid cut (IC 0.011, t=1.20, breakeven 4.12 bps). Diagnosis: illiquidity-
concentrated; `dollar_vol_20d` is the top feature by gain.

ADJUDICATE: does the signal survive **net of each band's OWN realistic overnight round-trip cost**
at a small **~$100K book**, in ANY pre-declared, actually-executable ADV-rank band? A MISS
everywhere (the signal lives only where its own cost exceeds it) is an honest, expected outcome.

## Pre-registered LIQUIDITY BANDS (fixed BEFORE results — NOT slid after)

By per-symbol 20-day-ADV RANK (rank 1 = most liquid), assigned per (symbol, day) point-in-time
using the SAME trailing `dollar_vol_20d`-driven rank Lane C's model already keys on. Bands declared
now, contiguous, non-overlapping, covering the universe rank 1 → ~5539:

| Band | ADV rank | rationale |
|------|----------|-----------|
| B1 | 1–500       | mega/large-cap, tightest spreads |
| B2 | 500–1000    | large-cap |
| B3 | 1000–2000   | mid-cap (straddles the t=1.20 top-1500 boundary) |
| B4 | 2000–4000   | small-cap |
| B5 | 4000–6000   | micro-cap tail (where Lane C says the signal lives) |

Each band is run INDEPENDENTLY through the unmodified harness (same features, same walk-forward
purge horizon_minutes=1440, same within-day shuffle canary, same NW lag=3, cadence_min=390,
n_folds=5, $1 price floor + per-day [0.5%,99.5%] winsorization ON — Lane C's anti-bad-print guard
stays). The L/S is the harness's dollar-neutral top/bottom-decile WITHIN the band.

## Pre-registered PER-BAND OVERNIGHT ROUND-TRIP COST MODEL (stated explicitly, BEFORE results)

The data has NO bid-ask quotes (daily OHLC + dollar-vol only). I therefore use a principled,
literature-backed daily HIGH-LOW spread proxy, NOT a guessed flat number:

- **Half-spread (per name, per day): Corwin–Schultz (2012) high-low bid-ask spread estimator**,
  computed point-in-time from the daily `rth_high`/`rth_low` (and the adjacent day for the 2-day
  overlap term), clamped to ≥0. This is the standard quote-free daily spread estimator. Each band's
  representative **one-way half-spread = the band's MEDIAN per-name Corwin–Schultz half-spread in
  bps** over the panel. (Median, not mean → robust to the illiquid tail's heavy right skew.)
- **Market-impact term:** at a $100K book a top/bottom-decile L/S holds ~1100 names → per-name
  notional ≈ **$90**. Against the SMALLEST band's ADV (rank ~5000 ≈ $0.5M/day) that is ~2e-4 of one
  day's volume → impact is negligible (square-root-impact at <1bp). Per-name positions this tiny do
  NOT move illiquid names. So the BINDING cost is the SPREAD, not impact — exactly the central
  failure mode. I add a fixed conservative **+1.0 bp one-way** generic slippage/impact/fees pad on
  top of the half-spread to avoid under-charging. (Capacity/fill is NOT the binding constraint at
  $100K; I verify per-name notional ≪ per-name ADV for every band and note it, but do not gate on it
  because at this size every declared band fills.)
- **Per-band one-way overnight cost** = `median_CS_half_spread_bps(band) + 1.0`. The round trip
  (MOC entry + next-open exit) is charged by the harness as cost × turnover; I compare the harness's
  per-band `breakeven_cost_bps` (ONE-WAY bps the gross edge absorbs) directly against this per-band
  one-way cost. The harness already double-counts entry+exit via turnover, so the one-way comparison
  is apples-to-apples and if anything conservative.

CRITICAL GUARD (declared): the edge must survive **each band's OWN cost** — the same illiquidity
that creates the signal also creates the cost. A band whose gross breakeven < its own one-way cost
is UNTRADEABLE even if its IC/t pass.

## CRITERION (FIXED BEFORE RESULTS) — TRADEABLE-AT-OUR-SIZE iff some pre-declared band has ALL of

1. **OOS rank-IC ≥ 0.01**, AND
2. **NW |t| ≥ 2.0**, AND
3. **net-of-its-OWN-cost L/S breakeven > 0**, i.e. the band's harness `breakeven_cost_bps`
   (one-way) **>** that band's own one-way cost `median_CS_half_spread_bps + 1.0` (gross edge
   strictly exceeds round-trip cost), AND
4. **positive NET Sharpe** at that band's own cost (re-run the harness L/S with
   `cost_bps_oneway = band's own one-way cost`; `sharpe_net > 0`), AND
5. the band is **actually executable at $100K** (verified: per-name notional ≪ band ADV; true for
   all declared bands at this size, so this leg is informational, not the binding gate).

VERDICT:
- **TRADEABLE NICHE** iff ≥1 band clears ALL of 1–5 → name the band, the net edge size, confirm it
  is executable at $100K → would motivate a strategy proposal to Lead/Ben.
- **SETTLED NULL** iff NO band clears all five → real signal, but it lives only where its own cost
  exceeds it; untradeable at our size.

NO threshold relaxation, NO band sliding, NO max-over-bands cherry-pick after seeing numbers. The
headline horizon is **1d** (Lane C's headline); 2d/3d are NOT re-litigated here. Single faithful run.
