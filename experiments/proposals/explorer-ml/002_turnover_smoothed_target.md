# 002 — Turnover-aware target: smooth the label to attack the economic gate directly

**Explorer:** explorer-ml
**Date:** 2026-06-12
**Lens:** Target engineering — the real problem is TURNOVER, not IC. Engineer the label so the
model learns the PERSISTENT component of the next-30m return, not the freshest tick.
**Status:** PROPOSED (awaiting Lead disposition)

## WHY (the failure mode this addresses)
The grind's airtight finding: the 30m signal IS ret_5m → "you're chasing the freshest tick" →
maximal turnover → breakeven ~1.4bps < ~2bps cost. The signal is REAL (clean canary, NW t~20)
but uneconomic FOR A STRUCTURAL REASON: the target rewards predicting fast-decaying micro-noise
that flips sign every period, forcing the L/S book to re-trade everything.

Standard rank/raw/vol_scaled/lambdarank labels all train on the SAME raw fwd_30m return, so they
all inherit its turnover. The lever nobody has pulled: change WHAT the model predicts so its
predictions are PERSISTENT across adjacent timestamps. If we train on a SMOOTHED target — the
EWMA of the forward return over the next few cadence steps, i.e. the slow component — the model
learns the part of the signal that survives more than one rebalance. Lower turnover at the cost
of some raw IC. The economic question is whether the breakeven RISES even if IC falls.

This is the cleanest "elegant > complex" target experiment: one transform, same harness, and it
attacks the exact mechanism (turnover) that kills every price signal. It is ORTHOGONAL to OFI
(OFI refines the signal; this reshapes the target) and to cost measurement (#5 measures cost;
this lowers required turnover) — so it composes with both.

## HYPOTHESIS (pre-registered, falsifiable)
Define the smoothed target at each (symbol, ts): an EWMA over the name's next K=3 cadence-step
forward returns (the fwd_30m label observed at ts, ts+30m, ts+60m for that symbol), half-life 1
step. Train the GBM on this smoothed target; **IC and all gates are STILL measured vs the RAW
fwd_30m return** (we never get to grade ourselves on the easier smoothed target).

1. (conf ~55%) Smoothed-target predictions have LOWER mean_turnover than the raw-target GBM
   (mechanical: smoother targets → smoother predictions → fewer rank flips). **Falsified if
   mean_turnover ≥ the raw-target GBM's turnover.**
2. (conf ~45%) Raw-return IC drops but stays positive and above canary (IC ∈ [0.012, 0.027]):
   the slow component is real but weaker. **Falsified if IC ≤ canary (signal destroyed) OR
   IC ≥ 0.027 (free lunch — implausible, would need scrutiny for a join bug).**
3. (conf ~40%, THE PRIZE) breakeven_cost_bps RISES above the raw-target ~1.4bps — turnover
   falls faster than gross return. **Falsified if breakeven ≤ 1.4bps** (smoothing helped IC-
   honesty but not economics → the turnover problem is intrinsic to the horizon, not the label).

Headline = **breakeven_cost_bps of the smoothed-target model vs ~1.4bps raw-target baseline.**

## METRIC (vs baseline)
Baseline = C11 raw-target GBM fwd_30m: IC 0.027, breakeven ~1.4bps, turnover (read from the
battery JSONL — the Lead has it). Report smoothed-target: IC vs raw return, NW t, canary,
gross/net/sharpe/breakeven/turnover, survivorship sharpe. Sweep K∈{2,3,5} and half-life∈{1,2}
as a SMALL grid (4 configs total, pre-committed — not an open search) to map the IC↔turnover
tradeoff curve; the Lead reads the frontier, not a single point (multiple-testing: report all 4,
no cherry-pick).

## GATES (all four)
1. Net-of-cost L/S (cost 2.0; report breakeven) — THE point of this experiment.
2. Shuffle-within-ts canary — CRITICAL HERE: smoothing pulls in returns from ts+30m/ts+60m
   for the SAME symbol, which look-ahead-uses FUTURE labels. This is LEGITIMATE for the TARGET
   (we are allowed to define any target from future returns — that's what a label IS), but the
   canary must still score ~0, proving the FEATURES (all strictly ≤ ts) carry no leakage. If
   the canary lifts off zero, the smoothing implementation leaked features and the run is void.
3. Label de-fragmentation: native 30m cadence; the K-step EWMA is computed per symbol along its
   own ts sequence (gaps at day boundaries handled by only averaging within-day forward steps —
   no overnight contamination of an intraday target).
4. Survivorship neutralization: per-symbol-demean OOS preds, re-run L/S.

## SPEC (Tier-2 standalone, ZERO rebuild)
`experiments/ml_turnover_smoothed_target.py`, module run, SET_VERSION=v1.1.1.
- Load panel via `load_panel`. Build the smoothed target ENTIRELY in python from the loaded
  (symbol, ts, raw_fwd_30m) triples: group by symbol, sort by ts, EWMA forward over the next K
  in-day cadence steps. Strictly a TARGET transform — features X untouched.
- Reuse the battery's walk-forward + 4 gates verbatim; only the training label changes.
- The canary uses the SAME `shuffle_within_groups` on the RAW y (not the smoothed target), so
  it remains a clean leakage arbiter on the features.

## WHAT WOULD MAKE ME DROP THIS
If turnover falls but breakeven stays ≤ 1.4bps across all 4 (K, half-life) configs, the verdict
is "the 30m horizon is intrinsically high-turnover; you cannot relabel your way out of it — the
fix must be a genuinely slower signal (60m+, or a slow feature family), or lower measured cost."
That sharpens the strategic ledger and retires the cheapest hope, which is worth knowing.
