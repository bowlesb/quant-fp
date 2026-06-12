# 004 — Multi-horizon composite target: blend 30m+60m to trade the SLOW component

**Explorer:** explorer-ml
**Date:** 2026-06-12
**Lens:** Target engineering — a fixed 30m horizon may be off-resonance with our cost structure.
A composite target (30m + 60m forward returns) biases the model toward the slower, more
persistent signal without abandoning the 30m information.
**Status:** PROPOSED (awaiting Lead disposition)

## WHY (the failure mode this addresses)
We have results at 30m AND 60m separately. 30m: IC 0.027, breakeven ~1.4bps, high turnover.
60m (W11 ret group): IC 0.008–0.014 — weaker, but at HALF the rebalance frequency, so its
TURNOVER per unit time is structurally lower and its breakeven-per-trade is mechanically more
forgiving. Neither horizon alone is economic, but they fail for OPPOSITE reasons: 30m has
signal but too much turnover; 60m has tolerable turnover but (alone) thin signal.

Nobody has asked whether a TARGET that BLENDS them — train on `0.5*fwd_30m + 0.5*fwd_60m`
(both demeaned/standardized so neither dominates by scale) — lets the model find predictions
that are good for BOTH horizons, i.e. the component of the 30m signal that PERSISTS into the
60m window. That persistent component is, by construction, the lower-turnover part of the 30m
signal. This is a different mechanism from 002 (which smooths along the FORWARD path of one
horizon); here we explicitly co-train on two real, separately-validated horizons and let the
model arbitrate. It directly tests "is the fixed 30m horizon off-resonance?"

## HYPOTHESIS (pre-registered, falsifiable)
Composite target Z = standardize(fwd_30m) + standardize(fwd_60m), per timestamp (so the blend
is scale-fair within each cross-section). Train GBM on Z. **Evaluate the SAME predictions
against the RAW fwd_30m return AND, separately, against the RAW fwd_60m return** — a prediction
that scores on both is the persistent signal we want.

1. (conf ~50%) Composite-trained preds keep meaningful fwd_30m IC (≥ 0.018) — we don't destroy
   the 30m signal by blending. **Falsified if fwd_30m IC < 0.012.**
2. (conf ~45%) When the composite preds are traded at the 60m rebalance cadence (lag=2,
   periods_per_year≈1638), breakeven_cost_bps EXCEEDS the 30m-native ~1.4bps — because the
   blend's persistent signal traded slower absorbs more cost. **Falsified if 60m-cadence
   breakeven ≤ 1.4bps.**
3. (conf ~40%) The composite at 60m cadence beats the PURE-60m-target model at 60m cadence on
   breakeven (blending IN the 30m information helps the 60m book). **Falsified if composite
   60m-cadence breakeven ≤ pure-fwd_60m breakeven.**

Headline = **composite-target breakeven at 60m cadence vs both (a) 30m-native ~1.4bps and
(b) pure-60m-target breakeven.** The win is: a target that is economic at the slower cadence
while retaining 30m-derived signal.

## METRIC (vs baseline)
Two baselines, both already in the log: (a) 30m raw GBM breakeven ~1.4bps; (b) pure fwd_60m raw
GBM (the Lead can read its breakeven from results.jsonl). Report composite: IC-vs-30m, IC-vs-60m,
NW t (each), canary, and L/S at BOTH cadences (30m and 60m) with gross/net/sharpe/breakeven/
turnover, plus survivorship sharpe.

## GATES (all four)
1. Net-of-cost L/S at BOTH cadences (the composite's purpose is to be tradeable at the slower
   one; report both so the turnover story is explicit).
2. Shuffle canary — the composite target mixes two FUTURE returns; both are legitimate label
   ingredients, but the canary on the (≤ts) features must still be ~0. A lifted canary means the
   60m label join pulled a leaked column → void.
3. Label de-fragmentation: 30m and 60m both use their native cadences; the blend is computed at
   each ts where BOTH labels exist (inner join on (symbol, ts) across the two horizons — rows
   missing either are dropped, not imputed, so we never fabricate a horizon).
4. Survivorship neutralization: per-symbol demean OOS preds, re-run L/S at the reported cadence.

## SPEC (Tier-2 standalone, ZERO rebuild)
`experiments/ml_multihorizon_composite.py`, module run, SET_VERSION=v1.1.1.
- Load the panel TWICE via `load_panel` (horizon="fwd_30m" and "fwd_60m"); inner-join on
  (symbol, ts) to get aligned (X, y30, y60). X is identical across horizons (same feature
  rows), so the join is just label alignment.
- Build Z = within-ts-zscore(y30) + within-ts-zscore(y60). Train GBM on Z. Predict OOS via the
  battery walk-forward. Run gates twice: realized=y30 @ 30m cadence, realized=y60 @ 60m cadence.
- Reuse battery `run_config` plumbing; the only novelty is the composite label and the
  dual-cadence evaluation (a thin wrapper around the existing L/S call with two periods_per_year).

## WHAT WOULD MAKE ME DROP THIS
If the composite's 60m-cadence breakeven ≤ 1.4bps AND ≤ pure-60m breakeven, then blending buys
nothing the pure horizons don't already offer — the horizons are economically additive at best,
and the "fixed 30m is off-resonance" hypothesis is FALSE (the resonance problem is cost, not
horizon choice). That cleanly retires multi-horizon target-blending as a lever and re-focuses
on OFI (refine the signal) + measured cost (#5). Honest null, ledger-sharpening.

## LEAD DISPOSITION — APPROVED (priority 3 of ml lens), 2026-06-12
Validated: gates present; dual-cadence evaluation + inner-join-no-impute is clean; distinct mechanism
from 002 (co-train on two validated horizons vs smooth one horizon's forward path). Lower priority than
001/002 only because it's more moving parts and partly overlaps 002's turnover goal — but the "is 30m
off-resonance with our cost structure" question is genuinely separate and worth one clean test. BUILD
ml_multihorizon_composite.py after ridge + smoothed-target. Report BOTH cadences (your spec). A null
(composite 60m-cadence breakeven <= both baselines) retires horizon-blending — ledger-sharpening.
ENQUEUE on delivery. (+ this adds to the global count; 001+002+004 = the ml lens's >=3 for the Monday bar.)
