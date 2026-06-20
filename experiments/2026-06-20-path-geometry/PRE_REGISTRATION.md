# PRE-REGISTRATION — Path-Asymmetry / Hölder-Roughness magnitude feature (`path_geometry` group)

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no code built yet)
**Vein:** structure-of-the-path (price geometry + volatility structure as MAGNITUDE features) — the
validated next vein from two independent signals: swing_dc's `dc_resp_chunk_slope` (the 1st replicated
non-null, a path-roughness fingerprint) AND the #255 tail-importance result (the model's profitable-tail
edge concentrates ~91% in price-return-shape + volatility structure, leaning ~2x harder at the tail).

This document COMMITS the thesis, the construction, and the full feature-utility gate BEFORE any feature
code is written. It is the contract the result is judged against — a null is a publishable answer.

---

## 1. The thesis (one concrete, falsifiable claim)

**Claim.** The *local geometric shape* of a name's recent intraday price path — specifically (a) its
**Hölder roughness** (how the path's range scales with the time window it is measured over, a fractal
exponent) and (b) its **directional path-asymmetry** (whether up-moves and down-moves have systematically
different local geometry: steeper-but-shorter vs shallower-but-longer legs) — carries **net-new
cross-sectional information about the MAGNITUDE of the next 30-minute move**, beyond what the name's own
trailing realized volatility already prices.

**Why this is plausibly net-new (not re-priced vol, not re-priced efficiency, not `resp_chunk_slope`).**
- It is NOT own realized vol: roughness is the *scaling exponent* of range-vs-window, a shape that is
  invariant to the overall vol level by construction (vol sets the amplitude; the exponent sets the
  texture). Two names with identical 30m sigma can have very different Hölder exponents.
- It is NOT `efficiency_ratio` (net displacement / gross path length): efficiency is a single-scale ratio;
  roughness is the *slope across scales* of how range grows, and asymmetry is a *sign-conditioned* shape —
  both orthogonal axes to a net/gross ratio.
- It is NOT `resp_chunk_slope` (swing_dc's DC scaling-law of leg-HEIGHT vs THRESHOLD): that slope is taken
  across the volatility-scaled DC threshold ladder at a fixed lookback; THIS is the scaling of realized
  range vs the measurement WINDOW (time), the classic generalized-Hurst/Hölder construction — a different
  operator on a different axis. The two may correlate; the gate's job (§4) is to prove this one adds IC
  beyond BOTH own-vol AND the swing_dc surface, or to null it honestly.
- The **asymmetry** half is genuinely new on this platform: no existing trusted group conditions a path's
  local slope/duration geometry on the SIGN of the local move at the cross-section.

**Direction discipline.** This is a **MAGNITUDE / risk feature** (predict |move| / vol-of-next-window /
range), NOT a direction feature. Direction is an 11-null graveyard; the validated vein is magnitude. The
gate's primary label is the forward 30m **realized range / |excess return|**, with forward signed excess
return reported only as a secondary (expected-null) check.

---

## 2. The construction (point-in-time, no-look-ahead, parity-portable)

Per name, at the sampled entry minute T (>=09:35 ET tradeable; ET via `convert_time_zone`, never raw-UTC
`.hour()` — pitfall #1), from the trailing intraday minute bars `[T-W .. T]` (W the lookback, e.g. 120m;
RTH-only, gap-safe per symbol):

**(A) Hölder / generalized-Hurst roughness.** For a geometric ladder of sub-windows tau in
{2,4,8,16,32,64} minutes, compute the mean absolute log-return aggregated at scale tau (the tau-minute
realized range proxy), regress log(range(tau)) on log(tau); the **slope = the roughness exponent H**
(H~0.5 random walk, H<0.5 mean-reverting/rough, H>0.5 trending/smooth). Parameter-free, scale-free.
Emit H plus the regression R^2 (how cleanly the path obeys a single power law = a robustness/cross-scale
flag, mirroring swing_dc's cross-scale-consistency noise filter).

**(B) Directional path-asymmetry.** Decompose `[T-W..T]` into up-legs and down-legs (sign of cumulative
move over a small fixed smoothing, or REUSE the swing_dc DC leg decomposition to stay on the same causal
spine). Emit the asymmetry of (median |slope|, median duration, count) between up-legs and down-legs —
e.g. `slope_asym = (med|up_slope| - med|down_slope|) / (sum)`. Sign-conditioned local geometry.

**(C) own-vol normalization, by construction.** Every amplitude-bearing emit is divided by the trailing
realized sigma (the same 30m sigma swing_dc uses) so the feature is a SHAPE, not a vol level — this is the
first line of defense against the "just re-priced vol-persistence" collapse (§4), built into the feature,
not bolted on at eval.

**No-look-ahead (the hard gate).** The feature at minute T reads ONLY bars <= T. The trailing-window
construction is causal; the CURRENT leg (for the asymmetry half) stays PROVISIONAL exactly like swing_dc's
last leg. **Bit-identical-when-future-bars-appended** is a REQUIRED test: computing the feature at T on a
frame truncated at T must equal computing it at T on the full-day frame (no future bar changes a past
value). The provisional current leg is allowed to differ only in that it is the current leg; all confirmed
history is frozen.

---

## 3. Production-portability note (decided BEFORE building — same pattern as swing_dc)

- **The Hölder/roughness half (A) is COLUMNAR** — a fixed-ladder regression of range-vs-tau over a trailing
  window is expressible as polars rolling aggregations + a closed-form OLS slope across the (small, fixed)
  ladder. → implement in **polars in the feature group**, parity-by-construction (live == backfill is the
  same expression on the same frame; no Rust needed, RT-trivial Layer-A).
- **The asymmetry half (B) is SEQUENTIAL-AND-HOT** if it uses a per-bar DC/zigzag leg state (each bar's leg
  state depends on the prior bar's). Two acceptable routes, both parity-safe:
  1. **REUSE the existing `swing_dc_fold` Rust kernel's leg outputs** (per-leg height/slope/duration are
     already emitted) and derive the up/down asymmetry columnarly from those — zero new kernel, rides the
     already-pinned causal spine. **PREFERRED** (no new hot path, maximal reuse).
  2. If a genuinely new sequential statistic is needed, add a **shared Rust kernel in `quant_tick`** pinned
     cell-for-cell by a **pure-Python reference oracle** (the exact swing_dc pattern:
     `tests/test_fp_*` oracle-pins-kernel + no-look-ahead + fold==reseed), called identically from live
     tape and backfill through ONE group so parity holds by construction.
- **No new data source.** Layer-A only (minute bars; per-leg trades/spread come free from `minute_agg`).
  Data axes are exhausted (memory: signal-source expansion nulled); this is pure geometry on data we have.

---

## 4. The feature-utility gate — PRE-COMMITTED pass/fail (all must hold to advance to a deploy test)

Substrate: trusted backfill store, top-N liquid per day, forward-30m cross-sectional EXCESS label,
`$1` floor on both legs, tradeable entry >=09:35 ET. Two DISJOINT date windows (train-window discover,
held-out-window replicate) — windows fixed in advance, no peeking.

**ORDERING PRINCIPLE (the swing_dc lesson — Lead amendment).** The binding constraint we keep failing is
INCREMENTAL-$-OVER-BASELINE, not IC. swing_dc passed trust + IC + replication and STILL $-nulled — and we
only discovered it at G7, AFTER building the whole Rust kernel + production group. So we FRONT-LOAD the kill
on the gate that actually binds: a cheap G0 $-screen runs FIRST, using zero-new-computation proxies, BEFORE
any production group or kernel work. G1-G6 (the full trust rigor) still run in full before any deploy — but
only if G0 says there is $ to chase. This saves the entire build when the vein is $-exhausted.

| # | Gate | Pass bar | Why |
|---|------|----------|-----|
| **G0** | **⭐ CHEAP EARLY $-SCREEN (runs FIRST, before any build)** | Construct the feature as the CHEAPEST possible eval-time proxy — **asymmetry via route B.1** (reuse swing_dc_fold's already-emitted per-leg height/slope/duration outputs → up/down asymmetry columnarly, ZERO new computation) + the **Hölder/roughness half as a handful of inline polars aggregations** (eval-time proxy, NOT a production group). Add the proxy to the FULL trusted-model baseline and run the EXACT Thread-1 harness $-curve test at {2,5,10}% cuts, vs the same baseline without it, dominating shuffle + predict-zero. **GO/NO-GO:** incremental $ over baseline at conservative cuts → proceed to G1-G6 + the production build. $-null (like swing_dc) → PUBLISH THE NULL, do NOT build the group/kernel, trigger the §6 pivot. | We keep failing at G7 after building. G0 is G7's question asked BEFORE the build, with throwaway proxies — fail fast on the binding constraint, save the kernel for an eval-afternoon. |
| G1 | **Own-vol control (CRITICAL)** | Partial rank-IC vs forward |move|/range, residualizing BOTH the feature AND the label on trailing realized sigma (and log-size), must retain **>= 60% of its raw IC magnitude** (collapse ratio <= 0.40 is a FAIL). | 10/13 prior "survivors" collapsed here — they were re-priced vol-persistence. This is THE IC gate. |
| G2 | **⭐ Incremental over the FULL trusted baseline (SHARPENED — Lead amendment)** | Incremental IC + non-marginal gain-importance of the new feature in a model that ALREADY contains the **full trusted baseline — explicitly the return-shape (price_returns, return_dynamics, efficiency) + volatility (volatility, ohlc_vol) groups AND swing_dc**. NOT "beyond swing_dc in isolation". The partial-IC must survive controlling for the baseline's own roughness/shape surface, not just for `resp_chunk_slope`. | The #255 + swing_dc evidence: the BASELINE'S OWN return-shape + vol groups already hold ~91% of the tail edge. swing_dc died on redundancy AGAINST THE BASELINE, not against itself. Beyond-swing_dc is necessary but NOT sufficient. |
| G3 | **Shuffle baseline** | Real per-date IC distribution must dominate the within-timestamp label-shuffle null (the feature's mean |IC| > 99th pct of shuffled). | The leakage/overfit null. |
| G4 | **BY-FDR** | Across ALL emitted features of the group, survive Benjamini-Yekutieli at q=0.10 (reuse `quantlib.battery.family.benjamini_yekutieli`, two-sided on the NW-t of per-date IC). | Multiple-testing honesty across the family. |
| G5 | **Disjoint-window OOS replication** | A feature that passes G1-G4 on window-1 must replicate (same sign, IC within a stated band, partial-IC still surviving own-vol) on the held-out window-2. | swing_dc earned trust ONLY because it replicated 9/9 on a disjoint window. |
| G6 | **No-look-ahead (bit-identical)** | Feature computed on the frame truncated at T == feature at T on the full-day frame, for confirmed history; current-leg provisional-only. Automated test. | A path-decomposition feature that repaints manufactures a fake edge (pitfall: standard zigzag). |
| G7 | **$-curve move (the deploy gate, only if G1-G6 pass)** | The PRODUCTION group added to the trusted-model inputs must IMPROVE the harness $-curve at conservative {2,5,10}% cuts vs the same baseline without it, dominating shuffle + predict-zero — the EXACT test applied to swing_dc in Thread 1 (and previewed at G0). | Trust + IC are necessary; moving the money curve is what justifies a fingerprint change. G0 previews this; G7 confirms it on the real production feature. |

**Decision rule.** Run **G0 FIRST** and report the $-screen result to the Lead BEFORE any group/kernel build.
Advance to the production feature-group build ONLY if G0 shows incremental $; then G1-G6 must pass on BOTH
windows; advance to a deploy proposal ONLY if G7 is green. Any single FAIL → publish the null with the
failing gate named,
and the vein-read stands (the *direction* was right even if this specific operator nulls). No p-hacking the
ladder/window: the {2,4,8,16,32,64} tau ladder and W=120m are FIXED here; if they are swept, the sweep is a
hyperparameter the FDR count must include.

---

## 5. What this is NOT (scope guards)

- NOT a direction feature (magnitude/risk only; signed return is a secondary expected-null check).
- NOT a new data source (Layer-A minute bars only).
- NOT a re-derivation of own-vol (G1 enforces) or of the baseline's shape/vol surface (G2 enforces).
- NOT built yet — G0 runs on throwaway proxies FIRST; no production group/kernel until G0 is GO.

---

## 6. PRE-COMMITTED PIVOT — what a G0/G7 null routes to (Lead amendment)

If G0 (or later G7) $-nulls, this is the **THIRD** path-structure-magnitude $-null on top of the current
baseline (the others: swing_dc's `dc_resp_chunk_slope`-as-$ in Thread 1; and the #255 read that the
baseline already holds ~91% of the path/vol tail edge). Three nulls on one vein is a verdict, not a miss:
**read it as "the path-structure-magnitude vein is $-EXHAUSTED on top of the current trusted baseline"** —
the baseline already prices price-geometry + volatility structure, so more clever geometry on the SAME
minute bars is redundant-by-construction.

**The pivot (named now so the null routes forward cleanly):** move to the genuinely ORTHOGONAL axis = the
now-queryable **deep QUOTE / TAPE microstructure** — spread dynamics and the liquidity-provision surface on
the quote tape (per the standing meta-conclusion: at our scale on bar-only data the remaining edge is
quote-dependent, e.g. the #205 spread re-test + liquidity-provision surface). That is a DIFFERENT data
substrate (the raw quote tape, not minute-bar geometry), so it is not redundant with the baseline's
bar-derived shape/vol groups by construction — the one axis the path-geometry nulls leave open.

A G0 GO, conversely, means the path-geometry vein has live incremental $ and we proceed to the full G1-G6
rigor + production build. Either outcome is a clean, forward-routing result.
