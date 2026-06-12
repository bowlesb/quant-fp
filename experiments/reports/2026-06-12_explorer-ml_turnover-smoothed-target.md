# Turnover-smoothed target: relabel to predict the persistent component, not the freshest tick

**Agent:** explorer-ml · **Date:** 2026-06-12 · **Proposal:** 002
**Status:** HARNESS BUILT + SMOKE-VALIDATED (mechanism confirmed); **full-depth run PENDING** for the
economic verdict. **Roadmap impact:** directly attacks the M3 economic gate (every price signal is
real but dies on turnover; breakeven ~1.4bps < ~2bps cost). If a smoothed config clears 1.4bps at
full depth, it is an M3 candidate with ZERO new data; if not, it cleanly retires "relabel your way
out of 30m turnover."

## 1. Hypothesis (pre-registered, before looking)
The 30m signal IS ret_5m (the grind's airtight finding) → chasing the freshest tick → maximal
turnover → uneconomic. Standard labels (raw/rank/vol_scaled/lambdarank) all train on the same raw
return and inherit its turnover. THE LEVER: train on a SMOOTHED target (forward EWMA over the next K
in-day cadence steps) so the model learns the part of the signal that survives more than one rebalance.
1. (~55%) Smoothed predictions have LOWER turnover. Falsified if turnover ≥ raw-target turnover.
2. (~45%) Raw-return IC drops but stays above canary (IC ∈ [0.012, 0.027]). Falsified if IC ≤ canary
   (signal destroyed) or ≥ 0.027 (free lunch — suspect a bug).
3. (~40%, THE PRIZE) breakeven RISES above ~1.4bps — turnover falls faster than gross return.
   Falsified if breakeven ≤ 1.4bps (turnover problem is intrinsic to the horizon, not the label).
LITERATURE PRIOR (cost-aware ML; Gârleanu-Pedersen "Dynamic Trading w/ Predictable Returns & Tx
Costs", NBER w15205; 2024-26 turnover-regularization work): a DOCUMENTED result — signal smoothing
via a 21-day MA cuts turnover ~82% and flips a strategy from net Sharpe -1.24 to profitable; gross
Sharpe declines (signal decay) but turnover reduction more than compensates. Strong support for the
mechanism. Translation: their MA is over DAYS; ours is intraday cadence steps (K=2-5 over 30-150 min)
— same logic, much shorter window suffices at our horizon.

## 2. Exploration (method / gates)
- DATA: v1.1.1, fwd_30m, price-only (19 feats).
- METHOD: per-symbol forward EWMA of the raw fwd_30m label over the next K in-day cadence rows (window
  truncated at the day boundary — an intraday target never averages across an overnight gap). Pre-
  committed grid (K, half_life) = (2,1),(3,1),(3,2),(5,2) + a k=1 baseline provably == raw. The model
  trains on the smoothed target; IC/L/S/survivorship are ALWAYS graded vs the RAW return.
- GATES: battery GBM fold loop reused byte-for-byte (label=raw path = identity transform on my pre-
  smoothed array). Canary shuffles RAW y (features-only leakage arbiter — the smoothed target
  legitimately uses future returns, but the FEATURES must stay clean).

## 3. Results (SMOKE 120d only — directional, not a verdict)
| config | IC | NW t | canary | breakeven | turnover | surv sharpe |
|---|---|---|---|---|---|---|
| raw (k=1, == GBM raw) | 0.0151 | 4.46 | -0.0092 | 0.98 | 2.76 | -4.92 |
| smoothed k2 hl1 | 0.0135 | 3.76 | -0.0092 | 0.80 | 2.46 | -4.68 |
| smoothed k3 hl1 | 0.0118 | 3.24 | -0.0092 | 0.66 | 2.34 | -4.82 |
| smoothed k3 hl2 | 0.0123 | 3.36 | -0.0092 | 0.95 | 2.19 | -4.21 |
| smoothed k5 hl2 | 0.0127 | 3.41 | -0.0092 | 1.25 | 2.02 | -2.52 |

## 4. Verdict + interpretation
**MECHANISM VALIDATED; economic verdict PENDING full depth.** The smoke confirms the core mechanic
and the harness:
- The canary is IDENTICAL (-0.0092) across all configs — correct by construction (it shuffles raw y,
  so smoothing the TARGET cannot move it) and matches the GBM raw canary on this window → harness clean.
- k=1 reproduces the GBM raw EXACTLY (IC 0.0151) → the baseline is faithful.
- H1 CONFIRMED (directional): turnover falls MONOTONICALLY with smoothing (2.76 → 2.02). H2 CONFIRMED:
  IC falls gently (0.0151 → 0.0127), all t>3.2, all above canary — signal not destroyed. Survivorship
  sharpe IMPROVES (-4.9 → -2.5) — smoother predictions are less survivorship-driven.
- H3 (the prize): the most-smoothed config (k5 hl2) breakeven 1.25 > baseline 0.98 — turnover fell
  faster than gross. Directionally toward the prize, but 120d is too short/noisy to claim it clears
  ~1.4bps. FULL DEPTH DECIDES.

Even a NULL at full depth (no config clears 1.4bps) is a clean, reportable result: the monotone
turnover drop + survivorship improvement is real, and "you cannot relabel your way out of the 30m
horizon's intrinsic turnover" sharpens the strategic ledger (the fix must then be a genuinely slower
signal — see 004 — or lower measured cost — see modeller's cost-by-liquidity verdict).

## 5. Next steps + declined follow-ups
- RUN FULL DEPTH (command ready, committed): `python -m experiments.ml_turnover_smoothed_target`.
  Heavy (5 GBM configs × 2 walk-forwards on ~4.8M rows) — sequence in a quiet window.
- FOLLOW-UP (conditional on a config clearing 1.4bps): re-gate that config under MEASURED cost via
  research.common_spreads_at_cadence rather than the flat 2bps — the smoothed signal trades the
  broad cross-section, so the cost-by-liquidity disjointness (modeller's finding) applies and must be
  checked before any M3 claim.
- DECLINED (parked, not this batch): a learned turnover penalty inside the objective (heavier, and
  the label-side smoothing is the elegant minimal test first — only escalate if smoothing's frontier
  is promising but doesn't quite clear cost).
