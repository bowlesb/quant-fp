# Data-lens report — ret_5m is a stable short REVERSAL (the structure the 0.027 headline hid)

**Author:** explorer-data | **Date:** 2026-06-12 | **Panel:** v1.1.1 (5.5M rows / 613 days / 785 syms)
**Status:** characterization complete; proposal 001 (Lead-approved) runs the gated net-of-cost verdict.

## One-line
The strongest, most stable price signal in the panel is a SHORT-HORIZON REVERSAL: rank high recent
5-min return → short, low → long. It's present in every liquidity tier and 29/30 months, persists to 60m,
and is strongest in CALM regimes. Whether it's net-of-cost tradeable is proposal 001 (prior ~25% no).

## Evidence (within-ts rank-IC of ret_5m vs forward return, non-NaN, excl 9:30 open)
- **Sign & stability:** univariate IC is NEGATIVE in 29 of 30 months (lone positive = partial current
  month). Many months t < −3. Strongest in the 2025 Mar-May tariff-vol period (IC −0.04 to −0.05).
- **Liquidity (full panel, two independent tier definitions agree):** roughly UNIFORM —
  q1 −0.025 (t−10.9), q2 −0.019, q3 −0.021, q4/most-liquid −0.020 (t−9.8). NOT illiquid-concentrated.
  (An early recent-window read suggested illiquid-only; corrected as a small-sample artifact.)
- **Persistence (not bid-ask bounce):** IC −0.0159 (30m) → −0.0092 (60m), ~58% retained. A real
  multi-minute reversal with a ~30-60min half-life, not within-minute bounce.
- **Time-of-day:** present at EVERY cadence 10:00-15:00 (t −1.4 to −5.2), strongest 14:30, weak only at
  the 11:00 midday lull. Pervasive, not a single-minute artifact.
- **Regime (the surprise):** reversal mean IC is MONOTONE in calm — disp-quintile q1(calmest) −0.0275 →
  q5(most volatile) −0.0150. Counterintuitive and actionable: a reversal book should size UP in calm
  regimes / filter high-dispersion days, NOT chase vol events.
- **Outlier days:** the top-20 most-negative DAYS (−0.11 to −0.18) are macro-vol events (2024-08-05
  carry-unwind; 2025 tariff cluster = 7 of top-20) but only ~3% of days → a pervasive baseline + an
  event-day amplification (high-mean, high-variance), not a few-day artifact.

## Relation to prior work
- vs the 0.027 multivariate headline: the headline LightGBM blends this reversal with momentum's
  continuation. The univariate reversal is the cleaner underlying structure (and the modeller's task-#5
  liquid-50 model showed +0.009 — opposite sign — because the blend netted reversal against momentum).
- vs task #5 ("ret_5m+position not tradeable on liquid tier, 30m cadence"): stands for the 30m model.
  The untested angle is a 60m-HOLD reversal (lower turnover) — proposal 001.

## Literature (Nagel 2012, "Evaporating Liquidity", RFS 25(7):2005-2039)
Short-horizon reversal returns = a PROXY FOR LIQUIDITY-PROVISION RETURNS; expected return is highly
predictable by VIX and SPIKES in turmoil (intermediaries withdraw liquidity → compensation rises).
- Resolves the apparent tension with my OBS6 (IC strongest in CALM): Nagel measures $ RETURN =
  ordering-quality × spread-captured. In turmoil the spread (compensation) is huge, so $ return is high
  even though my rank-IC (ordering quality) is noisier on turmoil days. My OBS5 (IC tail = turmoil days)
  is the Nagel-consistent half; my OBS6 (mean IC = calm) is the ordering-quality half. Both true.
- The deep implication: the reversal return IS the maker's spread compensation. A retail-latency
  cross-sectional reversal likely CANNOT capture it net of that very spread — a PRINCIPLED reason to
  expect 001 net-negative, not just "turnover too high." Proposal 001 now includes a Nagel VIX/regime
  split (via research.common_regime_labels disp_tier) to test whether the return concentrates in
  high-dispersion tiers exactly where the spread is widest.

## So what (for the edge hunt)
The reversal is real and robust but most likely uneconomic at retail latency/cost — it's the liquidity
maker's compensation. The honest value: (1) it's a clean documented "no" candidate that closes a real
question with evidence + a mechanism; (2) common_regime_labels (the calm-filter) is a reusable conditioner
for OTHER signals regardless of the reversal verdict; (3) it sharpens where to NOT look (don't try to
out-provide market makers at 30-60min on liquid names).

## Sources
- Nagel 2012, Evaporating Liquidity: https://academic.oup.com/rfs/article/25/7/2005/1602153
