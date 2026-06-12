# 005 — Quote-depth / spread-dynamics family from quote_agg (gated on M2 scale)

**Explorer:** explorer-features
**Date:** 2026-06-12
**Lens:** the resting-liquidity side of order flow — quote_agg, which the OFI bet ignores.
**Status:** PROPOSED — gated on quote_agg scaling 50→512 (Monday) + ≥10 days accrual.
**Cost tier:** Tier-2 once data accrues; data collection is already funded (M2 scale-up).

## WHY (mechanism story)
The Lead's order-flow bet (OFI v1.2.0) is the TRADE side — signed volume, what hit the tape.
`quote_agg_1m` carries the QUOTE side, which nobody is mining: mean/median spread_bps, mean bid
size, mean ask size, quote_imbalance. These are the *resting liquidity* dynamics, and they carry
information orthogonal to executed-trade OFI:

1. **Bid/ask SIZE imbalance** (mean_bid_size vs mean_ask_size) is the liquidity PROVIDERS'
   positioning — where depth is stacked. It LEADS the trade-side imbalance: depth builds on the
   side that will absorb/push the next move before the trades print. A different, earlier read on
   pressure than OFI.
2. **Spread COMPRESSION / widening** is the market-maker uncertainty signal. Spreads widen ahead
   of and during information events; a name whose spread is compressing into tightness is one MMs
   are confident in (continuation-friendly), one whose spread is blowing out is one to AVOID
   (the short-leg blow-up risk that killed the overnight book lives here).
3. **The cost-wall attack** (this is the strategic value): EVERY signal we found dies on the ~2bps
   cost. Spread_bps is the DIRECT, per-name, point-in-time measure of that cost. A spread-aware
   FEATURE lets the ranker prefer names that are cheap to trade RIGHT NOW — turning the cost wall
   from a flat strawman into a name-level conditioner. This connects directly to the cost-by-
   liquidity-tier work (task #5): that measures cost ex-post; this puts it in the model ex-ante.

## HYPOTHESIS (pre-registered)
{bid_ask_size_imbalance, spread_zscore_30, spread_trend_30} added to price-only raises breakeven_
cost_bps (the spread features let the model down-weight expensive names) AND the size-imbalance
feature carries directional rank-IC above its canary. I predict the COST/breakeven effect is the
stronger of the two — spread-awareness is more likely to help the cost gate than to be standalone
alpha at our latency (consistent with the advisor's "quote imbalance is a cost/feasibility signal,
NOT standalone alpha" prior — I'm testing it as exactly that conditioner).

## METRIC
Primary: breakeven_cost_bps (augmented vs price-only) on the quote_agg cross-section. Secondary:
size-imbalance standalone IC vs canary; whether a spread-FILTERED L/S (trade only names below a
spread percentile) lifts net sharpe vs the unfiltered basket; survivorship-neutralized sharpe.

## FALSIFICATION CONDITION
If neither the size-imbalance carries directional IC above canary NOR the spread features improve
breakeven / the spread-filtered net sharpe, the quote family adds nothing at our horizon — kill
it. If ONLY the spread-filter helps (cost conditioner works, no directional alpha), that is a
SUCCESS for its stated purpose (a cost gate) and should be handed to the production scorer as a
trade-eligibility filter, even though it is not "a feature that ranks names."

## DATA — GATE (why this is not runnable yet)
quote_agg_1m covers ~50 names × 2 days today (pure noise). Prerequisites mirror the OFI pilot:
(a) quote_agg scaled to ≥500 names (Monday M2 deploy); (b) ≥10 trading days accrued (~2-3 wks);
(c) at-scale quote parity proven on a settled session (QA invariant — the OFI proof is trade-agg;
quote-agg parity is separately unproven). Until then this is a noisy curiosity, not a verdict.

## CLOSE-MINUTE EXCLUSION (inherited)
Same ≥15:50 ET exclusion as OFI (Modeller's parity spec): the MOC/closing-cross window corrupts
live quote aggregation; no quote feature consumes minutes in [15:50, 16:00] ET, and no feature is
computed at a cadence ts ≥ 15:50. Pre-registered here so the family never touches the bad minutes.

## CODE SPEC (Tier-2, runnable when data accrues)
New module `experiments/family_h_quotes.py`, restricted to the quote_agg cross-section:
- **bid_ask_size_imbalance** = (mean_bid_size − mean_ask_size)/(mean_bid_size + mean_ask_size) at
  the cadence minute (cross-sectional pressure).
- **spread_zscore_30** = z-score of the name's median_spread_bps over the trailing 30 min vs its
  own trailing-20-day same-time-of-day spread distribution (self-normalized spread regime).
- **spread_trend_30** = slope of median_spread_bps over the trailing 30 min (compressing vs
  widening).
Augment price-only on the quote cross-section; run run_config {fwd_30m, fwd_60m} × {raw, rank}
baseline vs +family_h; ALSO run the spread-filtered L/S variant (trade only names with cadence
spread below the cross-sectional median). JSONL → experiments/family_h_results.jsonl.

## DISTINCTNESS
OFI = trade side (what executed). This = quote side (resting liquidity + cost). Complementary;
testing both isolates which side of the book carries the short-horizon signal at our latency.

## LEAD DISPOSITION
_(left for the Lead.)_

## LEAD DISPOSITION — APPROVED-AS-SPEC, BLOCKED on M2 scale + accrual, 2026-06-12
Validated as pre-spec: gates present, mechanism strong, and it correctly attacks the cost wall from
the EX-ANTE side (the model down-weights expensive names) — the complement to my task #5 ex-post cost
measurement. Distinct from OFI (quote/resting-liquidity side vs trade side). BLOCKER confirmed: quote_agg
is 50 names / 3 days (noise). Fires when: quote_agg scaled >=500 (Monday) + >=10 days accrued + at-scale
QUOTE parity proven (separate from the trade-agg OFI proof — flag to QA that quote parity is unproven).
The >=15:50 ET close exclusion you inherited is correct and binding. Build family_h_quotes.py when data
accrues. Your prior ("cost conditioner, not standalone alpha") matches the advisor's — test it as exactly
that. NOT counted against budget until it runs.
