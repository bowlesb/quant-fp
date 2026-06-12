# Cost-by-liquidity-tier: is ret_5m+position ALREADY tradeable on the liquid tier?

**Agent:** modeller (Research Lead) · **Date:** 2026-06-12 · **Task:** #5 (Manager top priority)
**Status:** COMPLETE — verdict declared. **Roadmap impact:** decided the M3 fork (single path = OFI).

## 1. Hypothesis (pre-registered, before looking)
THESIS: every price signal we found is REAL but dies on the ASSUMED ~2bps one-way cost
(ret_5m+position 30m breakeven ~1.4bps). The lever is COST, not signal. If our MEASURED half-spread
on the liquid tier is below breakeven, the existing signal is already an M3 candidate with zero new data.
PRE-REGISTERED PREDICTIONS (confidence): (1) ~70% trading-cadence median half-spread ~2-4bps, above
the 1.4bps breakeven; (2) ~60% even the most-liquid names straddle, not clear, breakeven; (3) PRIMARY
~65% ret_5m+position is NOT cleanly tradeable net-of-measured-cost even on the liquid tier; (4) ~80%
50 names / 3 days is too thin to be a verdict — directional only.

## 2. Exploration (method / data / gates)
- DATA: quote_agg_1m (50 captured liquid names, 3 days) for measured spread; v1.1.1 panel (613 days,
  4.84M rows) for the signal. SPY/QQQ excluded (intentional context ETFs, not equity universe).
- METHOD: (a) measure half-spread = median_spread_bps/2 at RTH 30-min cadence marks (10:00-15:30 ET);
  per-name median; count names below breakeven thresholds. (b) Re-gate the ret_5m+position carrier
  signal restricted to liquid-50 vs a seeded RANDOM-50 control (isolates liquidity from cross-section
  SIZE) vs the full 785-name panel; sweep flat one-way cost {1.0, 1.27, 1.4, 2.0, 2.7, 4.0} bps.
- GATES: within-ts rank-IC + NW t, shuffle canary, net-of-cost L/S, per-symbol survivorship demean.

## 3. Results (numbers, not adjectives)

Measured half-spread at RTH cadence, per-NAME median (50 names, SPY/QQQ excluded → 50 equities):
| threshold | 1.4bps | 2.0 | 3.0 | 4.0 | 5.0 |
|---|---|---|---|---|---|
| equities clearing it | 11 | 19 | 23 | 29 | 35 |
Median name half-spread ~3.1bps. Per-observation half-spread p25/p50/p75 = 1.27 / 2.70 / 5.66 bps.

Signal re-gate (613 days, rank label, ret_5m+vwap_dev+range_pct+gap_from_open):
| tier | names | IC | NW t | breakeven | canary | sharpe_net @1.0bps |
|---|---|---|---|---|---|---|
| full_panel | 785 | 0.0314 | 22.63 | 1.47bps | 0.0015 | +3.12 (pos ≤1.27bps) |
| liquid50 | 50 | 0.0091 | 4.15 | 0.82bps | 0.0048 | −0.53 (NEG at every cost) |
| random50 | 50 | 0.0170 | 7.76 | 0.47bps | 0.0020 | −1.88 (NEG at every cost) |

## 4. Verdict + interpretation
**NO — ret_5m+position is NOT tradeable on the liquid tier; the cost lever cannot rescue it.**
The signal and the tradeable-cost names are DISJOINT: the full-panel signal (IC 0.031, breakeven
1.47bps) lives in the BROAD cross-section — including the less-liquid names whose measured spread is
WIDE (~3bps median; only 11/50 equities <1.4bps). On the liquid tier where cost is low, the signal is
too WEAK (breakeven 0.82bps) to clear even an optimistic 1.0bps. random50 (0.017) > liquid50 (0.009)
at equal cross-section size ⇒ the drop is PARTLY liquidity-specific (efficient mega-caps have less
cross-sectional alpha), not just 50-name thinning. You cannot have cheap trading AND the signal.
Pre-registered prediction 3 (~65%) CONFIRMED.

## 5. Next steps + declined follow-ups
- CONSEQUENCE (roadmap): collapses the M3 fork — the cost lever does NOT open a second path. OFI is the
  SINGLE path: its job is reframed from "create signal" to "lift breakeven 1-2bps" (the steep
  11/19/23/29/35 cost-curve sets the bar precisely). Manager elevated this to the canonical statement.
- DECLINED: re-running on the full 500-name capture NOW (data not deep enough — revisit post-M2 deploy
  + multi-week accrual; that's the only thing that upgrades this from directional to verdict).
- FEEDS: research.common_spreads_at_cadence (registered) is the reusable cost table this produced.
- CAVEAT held throughout: 50 names / 3 days = directional; the real gate is the 500-name multi-week
  capture + exec's fill-prob curve (Monday).
