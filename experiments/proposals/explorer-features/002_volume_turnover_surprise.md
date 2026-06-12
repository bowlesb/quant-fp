# 002 — Volume / turnover surprise family (the panel has ZERO volume features)

**Explorer:** explorer-features
**Date:** 2026-06-12
**Lens:** the most glaring hole — the panel ranks names by price but never looks at HOW MUCH
they traded.
**Status:** PROPOSED (awaiting Lead disposition)
**Cost tier:** Tier-2 sandbox, ZERO new data collection (bars_1m volume + vwap already present).

## WHY (mechanism story)
Look at the 21 features: ret_*, vol_*, vwap_dev, range_pct, gap, momentum, calendar. **Not one
of them uses volume.** That is a striking omission for a short-horizon cross-sectional strategy,
because volume is the textbook proxy for *information arrival and attention*, and the empirical
volume→volatility→return lead-lag is one of the most replicated facts in microstructure.

The mechanism that matters for US cross-sectional ranking at our horizon:
- **Abnormal volume LEADS price moves.** A name trading at 3x its normal minute-volume is being
  re-priced; the move often continues for tens of minutes (the disagreement/information takes
  time to resolve). `ret_5m` captures the move that ALREADY happened; the volume surprise
  captures that a move is IN PROGRESS — a different, forward-leaning signal.
- **Dollar turnover** (volume × price) relative to the name's norm is the cleanest cross-
  sectional "attention" rank — it is comparable across names in a way raw share-volume is not.
- **Volume + return SIGN interaction** distinguishes continuation (high volume confirming a
  move) from exhaustion/reversal (a move on FADING volume). The panel cannot express this today.

This is the cheapest high-prior family I can propose: data is already there, the mechanism is
canonical, and it is fully orthogonal to the price-return features by construction.

## HYPOTHESIS (pre-registered)
Adding {vol_surprise_z_30, dollar_turnover_pctile_20d, signed_volume_ret_interaction} to the
price-only baseline raises breakeven_cost_bps on fwd_30m above the price-only baseline, with the
new features carrying real LightGBM gain importance (≥1 of the 3 in the top third) and the gain
NOT canary-explained. I expect volume-surprise to be the single most important addition of the
three (the attention/continuation channel).

## METRIC
- Primary: breakeven_cost_bps (augmented vs price-only).
- Secondary: within-ts rank-IC vs fwd_30m / fwd_60m, shuffle canary, per-feature LightGBM gain,
  survivorship-neutralized sharpe.

## FALSIFICATION CONDITION
If breakeven does not improve over baseline AND the volume features rank at the bottom of
importance, the "volume carries orthogonal cross-sectional signal at 30m" thesis is FALSE on
this panel — a genuinely informative negative (it would say the price features already
implicitly price the move and volume adds nothing at our horizon). Journal and stop. A canary-
contaminated lift (augmented IC gain ≤ canary) is also a fail.

## GATES (all four — reuse battery.run_config)
Shuffle canary; per-symbol survivorship demean; net-of-cost L/S (flat 2bps); turnover reported.

## DATA
bars_1m volume + vwap (backfill), already present for the full panel window. No collection.

## CODE SPEC (Tier-2 standalone)
New module `experiments/family_e_volume.py`. Per panel row (symbol, ts), point-in-time:
- **vol_surprise_z_30** = z-score of the trailing 30-min total volume vs the name's own
  distribution of 30-min volume over the trailing 20 trading days at the SAME time-of-day bucket
  (volume has a strong intraday U-shape; the same-bucket baseline removes the time-of-day
  confound so this is not a calendar feature in disguise).
- **dollar_turnover_pctile_20d** = (session-to-date dollar volume) rank within the name's own
  trailing-20-day session-to-date dollar-volume distribution at the same minute-of-day (0..1).
- **signed_volume_ret_interaction** = sign(ret_5m) × vol_surprise_z_30 — continuation when a
  move rides rising volume, reversal-flag when volume is fading. (Lets the model express the
  continuation/exhaustion split the panel cannot.)
Augment 19 price-only feats; run run_config for {fwd_30m, fwd_60m} × {raw, rank} baseline vs
+family_e. JSONL → experiments/family_e_results.jsonl.
Run: `docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_e_volume`

## LOOKAHEAD GUARD
Same-time-of-day baselines use only COMPLETED prior days; current-session terms use bars
at-or-before ts. NaN before 20-day warmup. No wall-clock time (ctx.ts only).

## NOTE ON DISTINCTNESS
This is NOT trade_agg OFI: OFI needs the tick stream (~50 names, scaling Monday). This family is
derivable from BAR volume across the FULL ~715-name panel TODAY — a wide-cross-section,
zero-collection complement to the narrow-but-deep OFI bet. If volume-surprise works on bars at
full breadth, it both stands alone AND de-risks the OFI thesis.

## LEAD DISPOSITION
_(left for the Lead.)_

## LEAD DISPOSITION — APPROVED (priority 1, tied — build FIRST), 2026-06-12
Validated: gates present; mechanism canonical (volume->vol->return lead-lag); data exists NOW (bar
volume+vwap, FULL ~715-name breadth); NOT a duplicate (the single most glaring panel hole — no volume
feature exists). This is the highest-prior features proposal: wide cross-section, zero collection,
fully orthogonal by construction, and a clean complement to the narrow OFI bet (de-risks it). BUILD
family_e_volume.py FIRST. Same imposed refinement: compare to the C11 battery baseline. The
same-time-of-day volume baseline (removing the U-shape) is exactly right — guards against it being a
calendar feature in disguise. ENQUEUE on delivery.
