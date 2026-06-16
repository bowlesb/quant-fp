# W4 — Method (overnight vs intraday decomposition, LIQUID portfolio)

## Data & panel
- Source: `/store/raw/bars` (1-min bars, 126 trading days 2025-12-15..2026-06-16, ~7,300 symbols with data).
- `build_panel.py` aggregates per (symbol, date), processing symbols in batches of 400 (memory-safe; the
  full-tree single scan OOM-killed at 12g).
- **RTH window, UTC-correct (RESEARCH_PITFALLS #1):** a bar is RTH iff its UTC minute-of-day
  (`hour*60+minute`, cast to Int32 — the naive Int8 product overflows and silently zeroes the filter) is
  `>= 810` (09:30 ET = 13:30 UTC) and `< 1190` (16:00 ET). `rth_open` = open of the FIRST such bar;
  `rth_close` = close of the LAST such bar.
- Per row also: `dollar_vol` = Σ(close·volume) over RTH (liquidity rank); `n_rth_bars`;
  `spread_bps` = median over RTH bars of (high−low)/close·1e4 — a **range-based round-trip cost proxy**
  (we have no quotes; the intrabar range generously overstates the bid-ask a marketable order pays,
  which is conservative for a cost gate). Liquid names: median ~8 bps, ~371 RTH bars/day (near-full).

## Universe
- `liquid500` = top 500 by median dollar-volume among names with ≥100 days. Cutoff ≈ $139M MDV — genuinely
  liquid. `megacap100` = top 100 (NVDA, TSLA, MSFT, AAPL, AMZN, …) as a sub-test.

## Returns
- Sort each symbol by date. `prev_close` = `rth_close.shift(1)`.
- `overnight = rth_open/prev_close − 1`; `intraday = rth_close/rth_open − 1`.
- Drop non-finite and |ret| ≥ 0.5 (split/data-error guard).

## Tests
1. **Descriptive** — pooled mean overnight vs intraday; per-name means → cross-sectional mean/median/%positive.
2. **Per-symbol demean (LOAD-BEARING).** Day-clustered t-test on the RAW level: each day's equal-weight
   cross-sectional mean of the component is one observation; t = mean/(sd/√n_days). Then subtract each
   name's OWN mean (`comp − comp.mean().over(symbol)`) and re-test — the residual daily mean must go to ~0
   if the raw level was pure per-name level (cycle-0's failure mode).
3. **Cross-sectional L/S.** On each rebalance date d, signal = the name's component realized at **d−1**
   (`shift(1).over(symbol)` — no look-ahead: yesterday's overnight is known before tonight's close→open
   bet; yesterday's intraday is known before today's open→close bet). Decile-rank by signal; MOMENTUM L/S =
   long top decile − short bottom decile on component[d]; REVERSAL = opposite. Equal-weight. Per-rebalance
   spread series is NON-OVERLAPPING (one obs per date).
4. **Tradeable entry.** Overnight bet = buy@today_close → sell@tomorrow_open; intraday = buy@open → sell@close.
   Each L/S leg charged its per-name range-based round-trip proxy (× cost_mult), measured and ×2 stress.
   **MOC/MOO auction-fill caveat:** the overnight bet's legs fill at the closing (MOC) and opening (MOO)
   auctions; we model them as the close/open print + the spread stress. A real auction can fill away from the
   print (especially on imbalance days); the measured-cost result is therefore an OPTIMISTIC bound.
5. **Gates.** Shuffle-canary (signal shuffled within date → edge must vanish); per-symbol demean (PRIMARY);
   walk-forward OOS (2nd half of dates, by date split); per-trade bootstrap (10k resamples of the
   per-rebalance NET series, 95% percentile CI); cost gate at measured proxy and 2×.
   **DECISIVE = demean-surviving AND OOS net-of-cost with bootstrap CI strictly > 0.**

Metrics: `spearman_ic`, `day_clustered_tstat` from `hf_metrics_fixed.py`; bootstrap hand-rolled (numpy).
polars+numpy only; all cross-sectional ops vectorized; per-date loop only for the decile L/S spread.
