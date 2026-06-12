# 001 — High-low realized-vol estimators + intraday range percentile

**Explorer:** explorer-features
**Date:** 2026-06-12
**Lens:** new features from data we already have but are DISCARDING (the bar high/low path).
**Status:** PROPOSED (awaiting Lead disposition)
**Cost tier:** Tier-2 sandbox, ZERO new data collection (bars_1m OHLC already present).

## WHY (mechanism story)
The panel's only volatility features are `vol_30m` / `vol_60m` — close-to-close standard
deviations of minute returns. That throws away the **intra-minute high/low path**, which
every realized-vol estimator since Parkinson (1980) uses because it is far more efficient:
- **Parkinson** (high-low range) is ~5x more efficient than close-to-close for the same window.
- **Garman-Klass** (open/high/low/close) is ~7-8x more efficient and partly corrects for drift.

Two distinct signals come out of the path, and BOTH are plausibly orthogonal to `ret_5m`
(the only feature the grind says carries the 30m signal):
1. **Better vol level** — a cleaner cross-sectional vol ranking. Not alpha by itself, but a
   conditioner and a denominator (it makes vol-scaled labels honest).
2. **Range PERCENTILE vs the name's OWN history** — today's GK range divided by the name's
   trailing-20-day median GK range. This is a self-normalized "is this name unusually active
   RIGHT NOW" signal. Information arrival (the precursor to a tradeable move) shows up as
   abnormal range BEFORE it shows up as a clean directional return — so the range-surprise
   may lead the cross-sectional return in a way `ret_5m` (already-realized) cannot.

The grind already exhausted realized RETURNS; nobody has tested realized VOLATILITY *structure*
as a cross-sectional ranker. This is the cheapest unexplored hole in the panel.

## HYPOTHESIS (pre-registered, before running)
Adding {parkinson_vol_30m, gk_vol_30m, range_pctile_20d} to the price-only baseline lifts the
within-ts rank-IC on fwd_30m ABOVE the shuffle canary AND raises breakeven_cost_bps versus the
price-only baseline by a non-trivial margin (target: breakeven up by ≥0.2bps — i.e. the new
features must move the COST gate, not just IC). The range-percentile feature carries non-trivial
LightGBM gain importance (top half of the augmented set).

## METRIC
- Primary: `breakeven_cost_bps` of the augmented set vs price-only baseline (the gate that
  actually decides tradeability — IC alone is known to mislead here).
- Secondary: mean within-ts rank-IC vs fwd_30m, shuffle-canary IC, LightGBM gain importance
  of the 3 new features, survivorship-neutralized sharpe.
- Run BOTH fwd_30m and fwd_60m (range-surprise may have a slower decay than returns).

## FALSIFICATION CONDITION (what kills this)
If the augmented breakeven does NOT exceed the price-only baseline breakeven (within run-to-run
noise), OR the new features sit at the BOTTOM of the importance ranking (LightGBM ignores them),
OR the lift is canary-contaminated (augmented IC gain ≤ canary), then high/low vol structure
adds nothing cross-sectional and the family is DEAD — journal it and move on. A pure vol-LEVEL
improvement with no breakeven gain is ALSO a fail for trading purposes (note it as a label-
denominator improvement only, hand to explorer-ml, do not pursue as a ranker).

## GATES (all four, non-negotiable — reuse experiments/battery.py:run_config)
Shuffle-label canary; per-symbol survivorship neutralization (demean); net-of-cost L/S backtest
(flat 2bps, same as the verdict battery for apples-to-apples); turnover reported. The new
features change the SCORE, not the label or universe, so turnover honesty is automatic.

## DATA
bars_1m (backfill source), already present for the full v1.1.1 panel window. No new collection.
Computed strictly point-in-time at each panel cadence ts from bars at-or-before ts.

## CODE SPEC (Tier-2 standalone, mirrors family_b_dispersion.py)
New module `experiments/family_d_highlow_vol.py`. For each panel row (symbol, ts):
- Pull the trailing window of 1-min bars (o,h,l,c) for that symbol up to ts (reuse the panel's
  own bar loader path; window = 30 and 60 min for the vol estimators, plus 20 trading days of
  daily GK ranges for the percentile denominator).
- **Parkinson_30m** = sqrt( (1/(4 ln2)) * mean( ln(high/low)^2 ) ) over the 30 1-min bars.
- **GK_30m** = sqrt( mean( 0.5*ln(high/low)^2 - (2 ln2 - 1)*ln(close/open)^2 ) ) over 30 bars.
- **range_pctile_20d** = today's session GK-range rank within the name's own trailing-20-trading-
  day distribution of daily GK ranges (self-normalized; 0..1). NaN until 20 days of history.
Augment the 19 price-only features with these 3; run `run_config` for {fwd_30m, fwd_60m} ×
{raw, rank} on baseline vs +family_d. Write JSONL to experiments/family_d_results.jsonl.
Run: `docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_d_highlow_vol`
(smoke: `-e SMOKE_DAYS=120`).

## LOOKAHEAD GUARD
All estimators use bars at-or-before ts only. The 20-day percentile uses COMPLETED prior days
plus the partial current session up to ts (no future bars). NaN before warmup — never a partial-
window placeholder (per CLAUDE.md "let it raise / NaN only for the undefined").

## LEAD DISPOSITION
_(left for the Lead: enqueued? duplicate? data exists? verdict.)_

## LEAD DISPOSITION — APPROVED (priority 1 of features lens), 2026-06-12
Validated: gates all present (canary/survivorship/net-of-cost/turnover); mechanism real (Parkinson/GK
are standard, range-percentile is genuinely novel here); data exists NOW (bars_1m OHLC, full panel);
NOT a duplicate (panel has zero realized-vol-structure features). Strong EV: it tests whether vol
STRUCTURE (not level) is an orthogonal cross-sectional ranker — untested. BUILD family_d_highlow_vol.py
(mirror family_b). One refinement I'm imposing: report the result vs the BATTERY price-only baseline
already in results.jsonl (C11 IC 0.027 / breakeven ~1.4bps) so the breakeven-lift claim is apples-to-
apples. Headline metric per your spec (breakeven lift) is correct — IC-only would not be enough.
ENQUEUE on script delivery. Global exp count tracked (multiple-testing).
