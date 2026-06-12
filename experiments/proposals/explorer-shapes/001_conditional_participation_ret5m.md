# Proposal 001 — Conditional participation on the existing 30m signal (SHAPE: sparse-participation overlay)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)
**Cost-structure rank: #1 (highest EV, cheapest).** Reuses existing predictions+labels — NO new data, NO new label.

## Hypothesis (mechanism story)
The 30m cross-sectional signal has real raw IC (~0.027-0.032) but dies net-of-cost *only because we
trade every name every timestamp* — turnover, not signal, is the killer (M1 verdict). The signal is
not uniformly informative: conviction (|prediction| magnitude / rank-extremity) and tradeability
(per-name half-spread) vary enormously. **If we participate ONLY on the timestamps where conviction
is high AND the name is in the cheap liquid head (<1.4bps half-spread), turnover collapses and the
average traded name is cheap — so the same signal can cross net-of-cost positive even though the
full-breadth version is negative.** This is the (A) low-turnover + (B) sparse + (C) long-biased
trifecta applied to the ONE signal we already have.

## What it is NOT
Not a new feature or model — a PARTICIPATION RULE (a strategy shape) wrapped around existing
predictions. The cost-by-liquidity script (modeller task #5) already has the per-name half-spreads;
this composes that tier-gate with a conviction-gate and measures net Sharpe vs participation rate.

## Label
EXISTING `fwd_30m` cross-sectional excess (and `fwd_60m` as a robustness horizon). No new label.

## Method
On the held-out period, for a grid of (conviction threshold q ∈ {top 30%, 20%, 10%, 5%}) ×
(liquidity tier ∈ {all, <1.4bps, <1.0bps}):
1. Trade only (symbol, ts) where |rank-centered prediction| ≥ q-quantile AND name ∈ tier.
2. Compute realized **participation rate** (fraction of name-timestamps traded), turnover, gross IC
   on the traded subset, and **net-of-cost** Sharpe using the per-name measured half-spread (not flat
   2bps) and the exec/risk fill-asymmetry haircut on the short leg.
3. Also run a **LONG-ONLY** variant (top-conviction longs in the cheap tier only) — sidesteps the
   short-underfill drag entirely.

## Pre-registered result that would FALSIFY
If net-of-cost Sharpe is ≤ 0 (or no better than the full-breadth -ve baseline) across EVERY
(conviction × tier) cell — i.e. sparsity + cheap-tier gating does NOT lift it above breakeven —
the "turnover-not-signal" rescue is dead for this signal and we stop chasing participation rules on
the 30m signal. Pre-registered prior: ~40% chance at least one cell crosses net-positive (the raw
IC is real and the cheap-tier cost is genuinely lower; but the signal is thin, so it may not).

## Gates (all present)
- **Shuffle canary:** re-run with shuffled labels on the SAME traded subset — net Sharpe must
  collapse to ~0 (else the gating itself leaks).
- **Survivorship neutralization:** per-symbol demean of the traded-subset returns → confirm the
  net edge (if any) is timing alpha, not surviving-name selection.
- **Net-of-cost:** per-name measured half-spread + short-leg fill-asymmetry haircut (NOT flat 2bps).
- **Turnover honesty:** report realized turnover AND participation rate per cell — a positive Sharpe
  at 0.1% participation is a different claim than at 50%.
- **Multiple-testing:** this is a grid (≤12 cells) — flag the cell count to the Lead for the global
  count; a single lucky cell is not a finding.

## Cheapness
★ — pure harness over existing predictions+labels+the cost script. No bar scan, no new label,
no new data. Runnable immediately.

## Lead disposition
<!-- Lead fills -->
