# W1 — Method

## Data & panel
- Source: `/store/raw/bars/symbol=<S>/date=<D>/data.parquet`, read-only, 125 trading days
  (2025-12-15 .. 2026-06-15; 06-16 excluded per spec). 7,671 symbol dirs → 7,333 symbols with ≥1 RTH close.
- **Time handling (RESEARCH_PITFALLS #1):** `ts` is genuine UTC. RTH window = UTC minute ∈ [810, 1190)
  = 13:30–19:50 UTC = 09:30–~16:00 ET. **Daily CLOSE = last RTH bar per (symbol, date).**
  (Bug caught & fixed: `dt.hour()*60` overflows polars `i8`; cast to `Int32` first.)
- Also aggregated RTH dollar-volume (Σ close·volume) per (symbol, date) for liquidity ranking.
- Cached panel: `close_panel.parquet` (826,045 rows). Built by `build_panel.py`.

## Universe
- **liquid500**: top 500 symbols by MEDIAN daily dollar-volume, requiring ≥95% date coverage.
- **megacap100**: top 100 by the same metric.
- Built inside `run_w1.py::select_universe` from the cached panel (deterministic).

## Spread measurement (`measure_spreads.py` → `spreads.csv`)
- For each liquid-500 name: sample up to 5 quote dates from `/store/raw/quotes/...`, take RTH quotes
  (same UTC-minute window), filter `ask>bid>0`, compute relative quoted spread `(ask-bid)/mid` in bps,
  median per date, then median across dates. This is the **round-trip** cost (crossing the full quoted
  spread = the cost of getting in or out once).
- All 500 names measured (100% coverage). Median = **7.34 bps**, p25=4.07, p75=11.38, min=0.48, max=44.39.
  (Wider than the 0.4–3 bps the hypothesis guessed — the top-500 tail includes liquid mid-caps.)
- Fallback (`DEFAULT_SPREAD_BPS = 5.0`) only used for names without quotes; 0 names flagged in liquid500.

## Portfolio construction (`run_w1.py`, vectorized numpy/polars)
- Close matrix `[T=125 × N=7333]`, NaN where missing.
- For each (F ∈ {21,42,63}, S ∈ {0,2}, H ∈ {5,10,21}, leg ∈ {decile=0.1, quintile=0.2}):
  - **Formation return** at rebalance t = `close[t-S] / close[t-S-F] - 1`.
  - **Forward H-day return** = `close[t+H] / close[t] - 1`.
  - **NON-overlapping rebalances**: t = F+S, F+S+H, F+S+2H, … (no overlap → independent periods).
  - Rank cross-sectionally; long top `leg`-fraction, short bottom `leg`-fraction, **equal-weighted**.
  - Per-rebalance gross = mean(long fwd) − mean(short fwd).
- **Cost:** at each rebalance, names entering OR exiting a leg pay their measured round-trip spread;
  charge = (traded fraction of the 2-sided book) × (mean spread of traded names). Net@1x = gross − cost;
  Net@2x = gross − 2·cost (stress).

## Gates
- **Shuffle-canary** (10 seeds): within each rebalance cross-section, permute the forward returns vs the
  formation ranks, rebuild legs, recompute L/S. Tests whether the ranking carries real information.
- **Per-symbol demean** (the decisive control): subtract each name's OWN mean forward H-day return
  (computed across all rebalances in the cell) before forming legs. Removes persistent per-name level
  effects → isolates a repeatable RANKING/momentum effect from "a few names just drifted up."
- **Walk-forward OOS:** first-half dates (t < 62) = train, last-half (t ≥ 62) = OOS. Decisive number is
  the OOS portfolio net-of-cost series.
- **Per-rebalance bootstrap:** resample the non-overlapping per-rebalance net@1x returns 10,000×,
  report 95% CI. CI must exclude zero ABOVE to pass.
- **Period-clustered t:** mean / (sd/√n) on the non-overlapping per-rebalance series (each rebalance is
  one independent observation — the honest clustering unit).

## Honest caveats (pre-committed)
- 125 days is SHORT for momentum. With non-overlapping rebalances the OOS half has very few periods:
  H5 → ~11–12, H10 → 5–6, **H21 → 2** (too few → OOS CI reported NaN, not a pass).
- Survivorship: current-universe only; delisted names absent. Per-symbol demean is the control for the
  level bias this induces.
- Libraries: polars + numpy only (no sklearn/scipy); spearman/bootstrap hand-rolled.
