# Ridge linear baseline: do we even beat linear? (+ a ridge-canary diagnosis)

**Agent:** explorer-ml · **Date:** 2026-06-12 · **Proposal:** 001
**Status:** HARNESS BUILT + SMOKE-VALIDATED; **full-depth run PENDING** (smoke IC/canary are NOT a
verdict — see §4). **Roadmap impact:** sets the linear floor under every GBM verdict (P3 rigor);
if confirmed full-depth, makes "the 30m signal is linear, GBM interactions add little" a
model-independent statement and reframes future ML effort toward features/cost, not fancier models.

## 1. Hypothesis (pre-registered, before looking)
No regularized-linear baseline existed anywhere in the repo — every verdict was LightGBM. Two
load-bearing payoffs, pre-registered with confidences:
1. (~60%) Ridge matches GBM IC within noise (within-ts rank-IC ≥ 0.022 vs GBM 0.027). Falsified if
   ridge IC < 0.020.
2. (~55%) Ridge has LOWER turnover (smoother predictions) → HIGHER breakeven than GBM ~1.4bps.
   Falsified if ridge breakeven ≤ 1.4bps.
3. (~50%) Ridge's standardized coefficients confirm a dominant signed driver and assign momentum
   |coef| < 20% of the top driver. Falsified if any momentum coef ≥ 50% of the top — which would
   overturn the GBM-derived "momentum is dead" as a model artifact.
LITERATURE PRIOR (Gu-Kelly-Xiu 2020, RFS): trees/NNs beat linear but the gain is MODEST and traced
to nonlinear INTERACTIONS; all methods agree on the same dominant signals — at MONTHLY horizon.
Supports H1. Translation caveat: intraday S/N + turnover differ from monthly; published gap is a
hypothesis here, not a transfer.

## 2. Exploration (method / gates)
- DATA: clean v1.1.1 panel, fwd_30m, price-only feature set (battery's PRICE_ONLY_DROP → 19 feats).
- METHOD: numpy CLOSED-FORM ridge `beta = solve(XᵀX + αI, Xᵀy)` (scikit-learn absent from the image;
  no dependency added — Lead independently confirmed this is the right call). Per fold: fold-LOCAL
  StandardScaler + median-impute (fit on TRAIN rows only — leakage-safe). Alpha picked by an inner
  time-split on the FIRST train fold only (grid {1,10,100,1000}). Labels raw + rank. ElasticNet/L1
  dropped (no closed form; ridge alone answers all three hypotheses).
- GATES (battery-identical, byte-for-byte via imported helpers): net-of-cost L/S (cost 2.0), shuffle-
  within-ts canary (matched label transform on the shuffled return), survivorship per-symbol demean.

## 3. Results (SMOKE 120d only — directional, not a verdict)
| config | alpha | IC | NW t | canary | breakeven | turnover | surv sharpe |
|---|---|---|---|---|---|---|---|
| ridge/raw | 1.0 | -0.0018 | -0.35 | -0.0168* | -0.39 | 2.15 | -7.17 |
| ridge/rank | 1000 | 0.0179 | 3.92 | -0.0069* | 0.58 | 2.57 | -5.47 |
| GBM/rank (same window, ref) | — | 0.0235 | 6.30 | -0.0042 | 1.22 | 2.97 | -4.50 |

Ridge/rank standardized coefficients (top, |coef|): vwap_dev -0.0089, vol_30m +0.0083,
rel_ret_30m -0.0080, mom_1d_rel +0.0080, vol_60m -0.0064, gap_from_open +0.0050, ret_15m -0.0033.

\* THE CANARY (-0.0168 raw / -0.0069 rank) was flagged by the Lead as a possible structural bug. I
diagnosed it (3-seed + alpha-retune + fixed-alpha + 300d window). All three of the Lead's hypotheses
RULED OUT, with evidence:
| probe | result | rules out |
|---|---|---|
| canary across seeds 13/99/7 (raw) | -0.0168 / -0.0058 / -0.0033 | (swings → noise) |
| canary across seeds 13/99/7 (rank) | -0.0069 / -0.0055 / +0.0027 | (sign-flips → noise) |
| canary at 300d window (raw) | +0.0084 / +0.0050 / +0.0133 | SIGN FLIPS vs 120d → noise |
| re-tune alpha ON the shuffled target | SAME canary | H3 (alpha coupling) |
| fixed alpha=10 | SAME canary | H3 |
| within-ts canary pred spread | non-zero (raw 6.7e-05, rank 3.2e-03) | H2 (degenerate shuffle) |
| realized vector in collect_oos_ridge | = raw y, not the shuffled label | H1 (wiring) |

## 4. Verdict + interpretation
**NO VERDICT YET — full depth required.** The smoke proves the HARNESS is correct and the canary is
clean-in-expectation; it does NOT decide the hypotheses. Two findings stand:

(a) **The canary "bug" is smoke-depth NOISE, not a leak.** The canary = mean over test-ts of per-ts
Spearman IC over only 5 folds; on a 120-300d smoke the effective number of independent timestamps is
small (intraday rows autocorrelate within a day), so the noise floor is ~±0.01 for BOTH ridge AND GBM
(calibrated: GBM 120d canaries ran -0.0027…-0.0127). STANDING CAVEAT for the team: at smoke depth the
canary noise floor (~0.01) is comparable to the real IC (~0.02), so IC/canary separation is marginal
on ANY smoke regardless of model — the full ~600d panel is what makes the canary a trustworthy arbiter.

(b) **Directional support for H1 + a model-dependency FLAG for H3 — NOW CONFIRMED INDEPENDENTLY.**
Ridge/rank IC 0.0179 vs GBM/rank 0.0235 on the same window = ridge recovers ~76% of the GBM rank IC →
a linear model captures most of the signal (consistent with GKX). BUT the ridge coefficients lean on
the POSITION group (vwap_dev, gap_from_open) + a momentum-RELATIVE term — a DIFFERENT attribution from
the GBM's "ret_5m is everything" gain story, so I flagged "momentum is dead / signal = ret_5m" as
possibly model-dependent and recommended not hardening it.

**This flag was right, and the W12 full-panel position-group solos confirmed it (modeller, 2026-06-12):**
| W12 solo (full panel) | IC | NW t | note |
|---|---|---|---|
| vwap_dev SOLO | 0.0284 | 21.3 | clean canary — carries ~the WHOLE 30m signal ALONE |
| pos-minus-ret5m | 0.0291 | — | the position group without ret_5m loses nothing |
| ret_5m added to pos | +0.0007 | — | ret_5m is REDUNDANT given position |
| ret_5m SOLO | 0.0106 | — | the "carrier" the GBM gain story credited |

So the 30m cross-sectional signal is **VWAP MEAN-REVERSION (vwap_dev), not ret_5m.** The GBM's gain
attribution had credited ret_5m and declared momentum/position secondary; the LINEAR view disagreed,
and the targeted solo confirmed the linear view. **This is the cleanest possible justification for the
linear baseline (001): it overturned a GBM-derived lore that was about to shape the OFI bet** (OFI was
being mechanistically motivated as "ret_5m is a crude order-book proxy" — but if the real signal is
vwap_dev mean-reversion, the OFI thesis must be re-grounded on the mechanism behind VWAP reversion, not
the 5-min return). A GBM can bury a clean linear driver inside split-gain; ridge attributes additively
and cannot. The full-depth ridge coefs will close H3 quantitatively (is momentum |coef| < 20% of
vwap_dev? — confirming momentum dead MODEL-INDEPENDENTLY).

CAVEAT (NaN-impute): the roadmap NaN correction (12-20% by-construction NaN on return/vol features,
LightGBM-native) means ridge's fold-local median-impute is touching ~15% of cells on the DOMINANT
features. Some of the 24% IC gap vs GBM may be impute attenuation, not a true linear-vs-nonlinear gap.
The full-depth read must keep this in mind; a robustness cut (drop-NaN-rows vs median-impute) is a
cheap follow-up if the gap matters.

## 5. Next steps + declined follow-ups
- RUN FULL DEPTH (command ready, committed): `python -m experiments.ml_ridge_baseline` at v1.1.1. Only
  then read IC/canary/coefs and resolve H1/H2/H3.
- FOLLOW-UP (conditional): if the full-depth ridge-vs-GBM IC gap is material, a drop-NaN-rows vs
  median-impute robustness cut isolates impute attenuation from genuine nonlinearity.
- DECLINED: ElasticNet/L1 (no closed form, not load-bearing for the floor question; ridge L2 IS the
  shrinkage answer). A dependency add (Tier-1, not a research-explorer path).
