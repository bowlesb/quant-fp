# W14 — method (exact operational definitions)

## Universe + data
- **LIQUID tier (PRIMARY)** = top 300 by dollar-volume, computed as average daily (vwap×volume) summed over
  the raw-trade window (2026-03-18 .. 2026-06-16, 63 sessions) from minute bars, restricted to symbols that
  have BOTH raw trades AND raw quotes AND ≥30 days of bars. ADV range: $414M (floor) .. $44B (SPY). Includes
  index ETFs (SPY, QQQ) — kept; they correctly fall in the NO-CATALYST cohort (no 8-K filings). File:
  `liquid_universe.csv`.
- Raw trades: `/store/raw/trades/symbol=<S>/date=<D>/data.parquet` (ts UTC, price, size). 63 sessions.
- Minute bars: `/store/raw/bars/...` (open/high/low/close/volume/vwap/trade_count), 378 sessions (18mo) on
  the liquid set — used for forward returns.
- Raw quotes: `/store/raw/quotes/...` — used only to measure the per-name half-spread for the cost gate.
- `filings` table (timescaledb) — 8-K/6-K `available_at` for the catalyst control.
- **TIME**: all ts GENUINE UTC. 13:30 UTC = 09:30 ET. RTH minute window [810,1190) = 09:30–15:50 ET; entry/
  burst-detection window starts at 815 (09:35, drop the open warmup). All `utc_minute` computed with an
  explicit Int32 cast (an Int8 `hour×60` overflow silently zeroed the RTH filter in an early draft — caught
  and fixed; RESEARCH_PITFALLS #1 time-handling discipline).

## Defining the "violent burst DAY" (the pre-committed event, refined for selectivity)
The pre-registration's minute-level `freq_z` = z-score of a minute's trade COUNT vs the trailing-30-min
rolling mean/std of that name's per-minute count. We compute exactly that (stage1, `freq_z`, k∈{2,3,4}).
**BUT** for a liquid megacap the within-day minute-level `freq_z` fires every single day on the open/close
volume U-shape (AAPL: peak `freq_z` 8–22 EVERY day) — so a minute-burst is NOT a selective daily event. The
pre-reg explicitly asks to "aggregate to a daily event: a name had a violent burst on day D". We therefore
define the **daily** event as a DAY-LEVEL activity anomaly vs the name's OWN recent norm (the attention/
info-shock proxy the hypothesis actually targets):
- `day_activity_z` = z-score of the day's total RTH trade count vs the symbol's trailing-10-day mean/std
  (strictly prior days, shift(1) — no look-ahead).
- `day_intensity_z` = z-score of the day's peak trades-per-second (max over RTH seconds) vs the same
  trailing-10-day baseline. This is the `microstructure_burst` peak-intensity idea, computed from raw trades
  (the feature store only has streaming `microstructure_burst` for recent dates, not the 63-day history).
- **burst_k** (k∈{2,3,4}) = `day_activity_z > k AND day_intensity_z > 1` (violent = anomalous COUNT **and**
  elevated peak intensity). `*_noint` variants drop the intensity gate (reference only).
This yields a SELECTIVE event (AAPL: 4 burst-days/63 at k2, e.g. 2026-04-07 z=4.9, 2026-05-01 z=6.3 — real
spikes, not the daily U-shape).

## Catalyst control (the primary confound)
For each (symbol, burst-day D): `has_catalyst` = TRUE iff an 8-K/6-K (form_type in {8-K,8-K/A,6-K,6-K/A})
for that symbol has `available_at` within ±1 calendar day of D. Split the cohort: **catalyst** (≈ PEAD/news,
the known effect) vs **no_catalyst** (the novel attention/activity signal — the headline). Base 8-K rate
across all liquid days = 12.3%; burst-day enrichment = 31% (k2) → 42% (k4): the most violent bursts are
disproportionately news-driven (a real, informative split).

## Forward returns + entry (no look-ahead)
- **Intraday decay (5min, 30min)**: from the burst PEAK minute, mid-close at peak_um → peak_um+5 / +30
  (next available minute bar ≥ target). Measures the within-day microstructure decay.
- **Multi-day (1d, 2d PRIMARY, 5d)**: a burst on day D is only fully known at D's close, so the trade ENTERS
  at the **next session open** (D+1, first bar ≥09:35 ET — tradeable, not the 09:30 print; Tradeable-entry
  trap rule) and EXITS at the close of D+h sessions. `fwd_2d` = close(D+2) / open(D+1) − 1.

## Scoring + gates (pre-registered, in order)
1. **Cohort drift** vs same-day non-burst controls, after **per-symbol demean** (subtract each name's mean
   forward return so the diff is within-symbol). Reported as burst_bps, ctrl_bps, diff_bps, Welch-t.
2. **Shuffle canary** — 200 seeds, permute the burst flag WITHIN each date, recompute the demeaned
   burst-minus-control diff; the real diff must lie OUTSIDE the 2.5/97.5 band.
3. **Walk-forward OOS** — split the 63 days 50/50 by date; pick the trade DIRECTION (sign) on TRAIN only,
   apply to OOS (hold-out rule); report OOS demeaned diff.
4. **Per-trade bootstrap** — OOS, signed by the train-direction, on NON-overlapping 2-day round-trips per
   symbol (a new trade requires ≥3 sessions since the prior entry so 2-session holds never overlap). 10k
   resamples; CI must exclude zero ABOVE zero to be a lead.
5. **Cost gate** — round-trip = 2 × measured per-name half-spread (median across ~8 sampled days of raw
   quotes; liquid median half-spread = 3.30 bps → ~6.6 bps round-trip), subtracted per trade; also a 2×
   stress. At a 2-day hold turnover is trivial, so cost is small — the explicit friction-wall fix.

## Compute
Sandbox only (`ops/sandbox.sh`, store mounted read-only), polars+numpy, vectorized. Stage1 (raw-trade burst
extraction, the heavy pass) caches one parquet/symbol; stage2 (daily panel) and stage3 (intraday rets) are
light; stage4 is the analysis. No production code touched; no live container exec.
