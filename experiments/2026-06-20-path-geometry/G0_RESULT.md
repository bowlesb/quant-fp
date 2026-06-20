# G0 cheap $-screen — path-geometry proxies — VERDICT: NO-GO ($-null on the binding constraint)

**Date:** 2026-06-20  **Gate:** G0 (the Lead-amended early $-screen — the binding constraint
incremental-$-over-the-FULL-baseline, run FIRST on throwaway proxies BEFORE any production group/kernel).

**Answer: NO-GO.** The path-geometry proxies do NOT produce a robust, significant incremental-$ improvement
over the full trusted baseline at conservative cuts. **Per the pre-committed decision rule: do NOT build the
production group/kernel — publish the null and trigger the §6 pivot to deep quote/tape microstructure.**

This is the third path-structure-magnitude $-null on top of the current baseline (swing_dc-as-$ Thread 1;
#255's read that the baseline already holds ~91% of the path/vol tail edge; this). The vein is $-exhausted
on top of the current bar-derived baseline — exactly the §6 pre-committed pivot trigger.

## What G0 tested (throwaway proxies — ZERO production code, NO kernel)
- **(A) Hölder/generalized-Hurst roughness** + R²: log-log slope of mean|aggregated log-return| vs a FIXED
  tau ladder {2,4,8,16,32,64}m over the trailing W=120m close path (pre-market 04:00 ET onward gives ~118
  bars before the 09:40 entry — verified). Own-vol-normalized by construction. H ranged 0.17–0.59 across
  names (sensible, varied).
- **(B) Directional path-asymmetry**: a causal vol-scaled DC zigzag over the window → up-leg vs down-leg
  median |slope|, median duration, leg-count asymmetry. Varied across names (slope_asym −0.59..+0.65).
- 5 proxy columns, **100% populated** at the entry. Added to the FULL trusted baseline (26 groups, 127 feats
  — explicitly incl. the return-shape + volatility groups, the G2-sharpened comparison). EXACT Thread-1
  harness path: purged walk-forward GBM (5 folds → 25 OOS days, 3,621 OOS rows), shared decide-core, per-name
  cost, shuffle + predict-zero. Identical rows both arms.

## The $-curve (OOS, net of cost). Δ = (baseline + proxies) − baseline

| cut | A baseline $ | B +proxies $ | Δ total $ | Δ prec | Δ Sharpe |
|----:|----------:|----------:|----------:|-------:|---------:|
| 2% | +16,586 | +394,784 | **+378,198** | +0.0351 | +16.08 |
| 5% | +212,799 | +208,124 | **−4,675** | −0.0029 | +0.27 |
| 10% | +104,989 | +102,284 | **−2,704** | −0.0056 | +1.07 |

- AUC: A 0.5288 → B 0.5262 (**lower** with proxies). rank-IC: A +0.0391 → B +0.0330 (**lower**).
- Headline 10% basket: A +$115,282 (Sharpe 21.5) vs B +$112,675 (Sharpe 22.6) — flat-to-slightly-worse.
- Shuffle baselines: B's shuffle is negative at every cut (good — no leakage); but the REAL B curve does not
  beat the real A curve except at 2%.

## Why this is a NO-GO, not a GO (the robustness kill)
The ONLY positive is the 2% cut (+$378k), and a per-day decomposition shows it is **not significant and
outlier-driven**:

| arm | 2%-cut per-day L/S excess | n_days | mean | median | **t** | max-day |
|-----|---------------------------|-------:|-----:|-------:|------:|--------:|
| BASE | | 19 | +45.5 bps | −86.8 bps | +0.61 | +756 bps |
| +PG  | | 19 | +190.3 bps | +63.0 bps | **+1.25** | **+2,660 bps** |

The +PG 2%-cut mean is dominated by a SINGLE day at +2,660 bps (a 26% one-day L/S excess); with only 19
test-days the per-day **t = +1.25 — not significant**. A genuine broad edge would (a) lift AUC + rank-IC
(both DECLINE here) and (b) show consistent gains across cuts (5%, 10%, 20% are all flat-to-negative). The
2%-cut blip is the same noisy-tightest-cut signature that flagged swing_dc — here even more outlier-driven.

## Cost saved (the point of G0)
Reached this verdict in ONE eval-afternoon with ZERO production-group code and ZERO Rust-kernel work — the
G0-first ordering (Lead amendment) front-loaded the kill on the gate we keep failing. G1-G6 (own-vol,
shuffle, FDR, OOS-replication, no-look-ahead) were NOT run: G0 is necessary-and-binding, and it failed.

## Routing forward (§6 pre-committed pivot)
Three path-structure-magnitude $-nulls on top of the current baseline = the vein is $-exhausted. Pivot to
the genuinely orthogonal axis: **deep QUOTE / TAPE microstructure** (spread dynamics + the liquidity-
provision surface on the now-queryable quote tape — the #205 spread re-test + LP surface). Different data
substrate (raw quote tape, not minute-bar geometry) → not redundant with the baseline's bar-derived
shape/vol groups by construction.
