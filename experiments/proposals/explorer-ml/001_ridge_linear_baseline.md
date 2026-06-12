# 001 — Regularized linear baseline: do we even beat ridge?

**Explorer:** explorer-ml
**Date:** 2026-06-12
**Lens:** ML methods — kill the LightGBM monoculture by establishing a linear floor.
**Status:** PROPOSED (awaiting Lead disposition)

## WHY (the failure mode this addresses)
Every result in EXPERIMENTS.md is LightGBM. We have NEVER established a regularized-linear
floor. This is a P3 gap (independent method development) and a silent risk to every verdict:

- The C11/W11 grind concluded "the 30m signal IS ret_5m, momentum is dead weight." That is a
  statement about a GBM's gain attribution. A GBM can bury a weak-but-real LINEAR contribution
  from a feature inside tree splits and report ~0 gain — so "dead weight at 30m" may be a
  property of the model, not the feature. A linear model attributes signal additively and
  cannot hide it. If ridge gives a different feature ranking, the "momentum is dead" finding
  is model-dependent and must be re-stated.
- If a plain ridge MATCHES the GBM's IC (~0.027), then the entire signal is LINEAR and the
  GBM's nonlinearity/interactions buy us nothing on this panel. That is a load-bearing fact:
  it means future ML effort should go to FEATURES and COST, not to fancier models — and it
  makes the cheap, fast, deployable linear model the production default (lower latency, no
  overfit surface, trivially interpretable coefficients).
- If ridge BEATS the GBM net-of-cost (plausible: linear predictions are smoother across
  adjacent timestamps -> lower turnover -> higher breakeven), that is directly the economic
  lever the whole org is chasing.

## HYPOTHESIS (pre-registered, falsifiable)
On the clean v1.1.1 panel, fwd_30m, price-only feature set (the battery's PRICE_ONLY_DROP set,
~17 features), within-the-same-walk-forward-folds:

1. (conf ~60%) Ridge matches GBM IC within noise: ridge within-ts rank-IC ∈ [0.022, 0.030]
   (i.e. ≥ 0.022, not materially below the GBM's 0.027). **Falsified if ridge IC < 0.020.**
2. (conf ~55%) Ridge has LOWER turnover than the GBM and therefore a HIGHER breakeven_cost_bps
   than the GBM's ~1.4bps. **Falsified if ridge breakeven ≤ 1.4bps** (no turnover advantage).
3. (conf ~50%) Ridge's standardized coefficients confirm ret_5m as the dominant signed driver
   AND assign |coef| to momentum features that is < 20% of the ret_5m coefficient (corroborates
   "momentum weak"). **Falsified if any momentum coef ≥ 50% of |ret_5m coef|** — which would
   overturn the GBM-derived "momentum is dead" finding as a model artifact.

The headline number is **(ridge breakeven_cost_bps − GBM breakeven_cost_bps)**: does a linear
model buy us economic room the tree model's turnover throws away?

## METRIC (vs baseline)
Baseline = C11 GBM price-only fwd_30m: IC ≈ 0.027, NW t ~20 (depth), breakeven ~1.4bps,
clean canary. Report ridge: within-ts rank-IC, NW t, shuffle canary, net-of-cost L/S
(gross/net/sharpe/breakeven/turnover), survivorship-demeaned sharpe, and standardized coefs.
ElasticNet(l1_ratio∈{0.1,0.5}) as a secondary cut for sparsity/feature-selection read.

## GATES (all four, identical to the battery)
1. Net-of-cost L/S backtest at cost_bps_oneway=2.0 (and report breakeven).
2. Shuffle-within-timestamp canary (the SAME `shuffle_within_groups(y, ts, SEED=13)`); a clean
   linear harness must score ~0. This is the leakage arbiter and ALSO catches standardization
   leakage (scaler must be fit on TRAIN folds only).
3. Label de-fragmentation: native 30m cadence (this proposal is 30m only).
4. Survivorship neutralization: per-symbol-demean OOS predictions, re-run L/S; report sharpe.

## SPEC (Tier-2 standalone, ZERO panel rebuild — mirrors family_c structure)
`experiments/ml_ridge_baseline.py`, run as a module in the experimenter container:
```
docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.ml_ridge_baseline
```
- Reuse `quantlib.research.load_panel`, `walk_forward_folds`, `per_timestamp_ic`,
  `mean_ic`, `newey_west_tstat`, `shuffle_within_groups`, `long_short_backtest`, and the
  battery's `per_symbol_demean` / `filter_smoke` / PRICE_ONLY_DROP — so gates are byte-identical.
- Model: `sklearn.linear_model.Ridge(alpha)` and `ElasticNet`. Per fold: fit
  `StandardScaler` on TRAIN rows only, transform test; impute NaN feature cols with the
  TRAIN column median (GBM handles NaN natively, linear does not — median-impute is the
  honest minimal choice and must be fold-local to avoid leakage). Tune alpha by a small
  fixed grid {1,10,100} on an inner split of the first train fold only (no test peeking).
- Predictions feed the IDENTICAL `collect_oos`-style path and the 4 gates.

DEPENDENCY NOTE for the Lead: confirm `scikit-learn` is in the experimenter image. If not,
the equivalent closed-form ridge is ~5 lines of numpy (`(XᵀX + αI)⁻¹ Xᵀy`) with no new dep —
I will spec the numpy version on request so this never blocks on packaging.

## WHAT WOULD MAKE ME DROP THIS
If ridge IC < 0.020 AND breakeven ≤ 1.4bps AND coefs just re-confirm ret_5m dominance with
momentum near-zero, the finding is "GBM is fine, momentum really is dead, linear buys nothing"
— a clean, valuable NULL that hardens the OFI/cost thesis. Honest either way; this is a
floor-setting experiment whose null is as useful as its hit.

## LEAD DISPOSITION — APPROVED (priority 1 of ml lens, build FIRST), 2026-06-12
Validated: gates byte-identical to the battery (good); data exists; NOT a duplicate (zero linear results
exist — a real P3 gap). LOAD-BEARING for honesty: every "momentum is dead / signal is ret_5m" finding is
a GBM gain-attribution claim; a ridge with additive coefficients is the independent check that it's not a
model artifact. Three outcomes all valuable (ridge matches=signal is linear, GBM nonlinearity buys
nothing; ridge beats net-of-cost=smoother preds=lower turnover=the cost lever; ridge differs on
momentum=overturns a standing finding). DEPENDENCY: confirm scikit-learn in the experimenter image — if
absent, use the numpy closed-form ridge ((XtX+aI)^-1 Xty), NO new dep, so this never blocks on packaging.
Fold-local scaler + median-impute (you specified this — critical for a clean canary). BUILD
ml_ridge_baseline.py FIRST. ENQUEUE/run on delivery.
