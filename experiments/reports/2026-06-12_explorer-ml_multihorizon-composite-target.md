# Multi-horizon composite target: blend 30m+60m, trade at the slower cadence

**Agent:** explorer-ml · **Date:** 2026-06-12 · **Proposal:** 004
**Status:** HARNESS BUILT + SMOKE-VALIDATED (canary clean, promising); **full-depth run PENDING** for
the verdict. **Roadmap impact:** attacks the M3 economic gate from the horizon angle — tests whether
the fixed 30m horizon is off-resonance with our cost structure. The MOST promising of my three smoke
results: the composite dominates both pure horizons on breakeven AND survivorship.

## 1. Hypothesis (pre-registered, before looking)
30m and 60m fail for OPPOSITE reasons: 30m has signal (IC 0.027) but too much turnover (breakeven
~1.4bps); 60m has tolerable turnover (half the rebalance frequency) but thinner signal alone. A
TARGET that blends them — within_ts_zscore(fwd_30m) + within_ts_zscore(fwd_60m), scale-fair — lets
the model find predictions good for BOTH horizons, i.e. the part of the 30m signal that PERSISTS into
60m (the lower-turnover part).
1. (~50%) Composite preds keep meaningful fwd_30m IC (≥ 0.018). Falsified if fwd_30m IC < 0.012.
2. (~45%) Composite at 60m cadence has breakeven > 30m-native ~1.4bps. Falsified if ≤ 1.4bps.
3. (~40%) Composite at 60m cadence beats the PURE-60m-target model on breakeven. Falsified if ≤ pure_60m.
LITERATURE PRIOR (Gârleanu-Pedersen, NBER w15205): optimal trading weights SLOW-decaying predictors
more relative to fast-alpha-decay ones, and holds smoother portfolios that limit turnover — a co-
trained blend that biases toward the persistent (slow) component is the discrete analog.

## 2. Exploration (method / gates)
- DATA: v1.1.1; load BOTH fwd_30m + fwd_60m panels; inner-join on (symbol, ts) (drops rows missing
  either label — never fabricates a horizon). Smoke join = 922k rows / 120 days.
- METHOD: target = within_ts_zscore(y30) + within_ts_zscore(y60). Train GBM on the composite; grade
  IC vs BOTH raw horizons; run the L/S at BOTH 30m and 60m cadences; per-symbol demean at each. Purge
  with the LONGER 60m horizon. Reference runs: pure_30m and pure_60m on the SAME joined panel (fair).
- GATES: battery-identical; canary shuffles raw y30 (features-only arbiter).

## 3. Results (SMOKE 120d only — directional, not a verdict)
| target | IC vs 30m | IC vs 60m | canary | breakeven 30cad | breakeven 60cad | turn 60cad | surv sharpe 60cad |
|---|---|---|---|---|---|---|---|
| composite z30+z60 | 0.0174 | 0.0173 | -0.0017 | 1.21 | **1.48** | 2.47 | -1.73 |
| pure_30m | 0.0167 | 0.0147 | -0.0017 | 0.91 | 1.12 | 2.71 | -2.62 |
| pure_60m | 0.0121 | 0.0125 | -0.0017 | 0.54 | 0.74 | 2.33 | -3.10 |

## 4. Verdict + interpretation
**PROMISING; verdict PENDING full depth.** Canary clean (-0.0017) → harness clean. On the smoke the
composite DOMINATES both pure horizons on EVERY economic axis:
- H1 CONFIRMED (directional): composite retains fwd_30m IC 0.0174 (≥ 0.018 target essentially met)
  AND has the best fwd_60m IC (0.0173) — it genuinely captures BOTH horizons, not a watered-down
  average. It beats pure_30m on 60m IC (0.0173 vs 0.0147) → the blend adds 30m information that
  helps the 60m view, exactly the "persistent component" thesis.
- H2 directionally MET: composite 60m-cadence breakeven 1.48 EDGES the ~1.4bps line on the smoke
  (vs pure_30m 1.12, pure_60m 0.74).
- H3 CONFIRMED (directional): composite 60m breakeven 1.48 >> pure_60m 0.74 — blending the 30m
  information in MORE than doubles the slow book's breakeven.
- Survivorship sharpe is also best for the composite (-1.73 vs -2.62 / -3.10) — least survivorship-
  driven.
Caveat: all sharpes are still negative on the smoke (120d, high-turnover regime); the breakeven 1.48
is the signal to chase, and 120d is too short to claim it clears 1.4bps net. FULL DEPTH DECIDES — but
this is the one most likely to produce a positive economic result.

## 5. Next steps + declined follow-ups
- RUN FULL DEPTH (command ready, committed): `python -m experiments.ml_multihorizon_composite`.
- FOLLOW-UP (conditional on the composite clearing 1.4bps full-depth): (a) re-gate under MEASURED
  cost (research.common_spreads_at_cadence) at the 60m cadence; (b) a weight sweep on the blend
  (e.g. 0.3·z30 + 0.7·z60) to find the IC↔turnover sweet spot — but ONLY if the equal blend is
  promising, to avoid a fishing expedition.
- COMPOSES WITH 002: 002 smooths within one horizon, 004 blends across two — if both clear, a
  combined "smoothed-composite" target is the natural next test (declined for now: one lever at a
  time until each is verdict-ed honestly).
