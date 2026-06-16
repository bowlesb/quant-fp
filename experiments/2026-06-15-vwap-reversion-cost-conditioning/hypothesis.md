# Hypothesis (PRE-REGISTERED — written before any run) — H1: vwap_dev reversion, cost-conditioned

**Author:** Modelling Agent · **Date:** 2026-06-15 · **Resource:** CPU-only (proof-of-loop, bounded).

## Idea
The only proven cross-sectional carrier is `vwap_dev` mean-reversion (high deviation from intraday VWAP →
lower forward return). It is REAL (IC ~0.028, t~21, model-independent) but UNECONOMIC: avg breakeven
~1.4–1.66 bps < ~2 bps cost at turnover ~3.2/period. We do NOT hunt a new signal — we attack the COST WALL.
The proof-of-loop test asks the FIRST sub-question of H1, the cheapest honest read available in this stack:

> **Is the vwap_dev reversion signal cross-sectionally CONCENTRATED in liquid (low-cost) names, or is it
> uniform across the liquidity spectrum?** If it is concentrated in liquid names, a liquidity-gated +
> hysteresis variant has a real chance to clear cost (H1 worth full study). If the signal is uniform or
> STRONGER in illiquid names, H1's cost-conditioning thesis is structurally doomed (the signal lives
> exactly where cost is worst) and H1 should be down-ranked.

## What we actually test in the proof (bounded, one quick test)
Because this stack currently holds **only today's single live RTH session** (no multi-day panel — the deep
battery DB is absent), the proof is a **single-session, within-minute cross-sectional read**, explicitly
NOT a tradeable backtest. We measure, on today's RTH cross-sections:
1. The **rank correlation between `vwap_dev` (at minute t) and forward 5–15 min return** (the reversion
   sign), pooled within-minute — confirming the carrier is present and NEGATIVE in live data.
2. Whether that within-minute reversion IC **differs between a high-liquidity and a low-liquidity half**
   of the cross-section (liquidity proxied by dollar-volume / trade activity available in the store).

This is a sign-and-concentration probe, not an edge claim. One session = noisy; the verdict is directional.

## EXPECTED result (committed BEFORE running — the falsifier)
- **Primary (confidence ~65%):** within-minute `vwap_dev`→forward-return rank-IC is **NEGATIVE** (reversion)
  and of order **−0.01 to −0.05** pooled, consistent with the multi-day finding. A POSITIVE or ~zero pooled
  IC on a real RTH session would contradict the entire standing carrier story and is the key falsifier.
- **Secondary (confidence ~50%, the load-bearing one for H1):** the reversion is **NOT meaningfully
  stronger in the illiquid half**. I expect roughly comparable magnitude in both halves (≤ ~1.5× ratio),
  OR stronger in liquid names. If the reversion is clearly **STRONGER in illiquid names** (e.g. illiquid IC
  > 2× liquid IC), that is EVIDENCE AGAINST H1 — the signal lives where cost is worst — and H1 drops in rank.
- **Honest caveat pre-committed:** a single Monday session, off the deep panel, cannot confirm OR refute
  economics. A null/ambiguous single-day read is an ACCEPTABLE outcome and means "blocked on the multi-day
  panel," not "H1 dead." The only thing that down-ranks H1 from this probe is a CLEAR illiquid-stronger sign.

## Method pointer
See `method.md` (data = live `/store` parquet, today; features = `vwap_dev` from `price_volume` or
`multi_day_vwap`, forward return from `price_returns`/bar closes; liquidity = dollar-volume / trade_count).
Read-only. No code edits. If a missing feature is wanted, it goes through a PR per `docs/PR_WORKFLOW.md`.
