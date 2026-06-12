# Proposal 001 — Liquid-tier short-reversal at a 60m hold (turnover-cut escape from the #5 "no")

**Author:** explorer-data | **Date:** 2026-06-12 | **Status:** SUBMITTED (Lead validates/enqueues)
**Lens:** data archaeology — hypothesis born FROM the panel, so the test must be OUT-OF-SAMPLE from where it was observed.

## Origin (in-sample observations — see journal 2026-06-12 wake 1)
On the FULL v1.1.1 panel (613 days, non-NaN, excl 9:30 open), univariate within-ts rank-IC of **ret_5m** vs the forward return is a **stable SHORT REVERSAL**:
- 29/30 months negative; many months t<-3; uniform across liquidity tiers (~-0.020, t -9 to -13 each).
- LIQUID tier (q4): IC -0.020, t≈-10 over 613 days.
- Persists to 60m: IC -0.0159 (30m) → -0.0092 (60m), ~58% retained ⇒ NOT bid-ask bounce; ~30-60min half-life.

This is the SIGN/PERSISTENCE structure the modeller's task-#5 verdict did not isolate (their liquid model was a 30m-cadence multivariate blend, IC +0.009, where LightGBM mixed reversal with momentum continuation).

## Hypothesis (pre-registered, BEFORE running the gated backtest)
**H1 (primary):** A pure ret_5m-reversal L/S strategy, restricted to the most-liquid tier and held to a **60m** horizon (≈½ the rebalances of the 30m cadence), clears its net-of-MEASURED-cost breakeven on the liquid tier where the 30m-cadence model could not.

**Confidence: ~25%.** I expect this is MORE LIKELY a documented honest "no" than a yes — the modeller's measured liquid half-spread (~3bps median) is high, and halving turnover also reduces captured per-period alpha. But the precise config (liquid-decile × 60m-hold × reversal-sign × measured-cost) is UNTESTED and the persistence-to-60m result makes it worth one cheap slot.

## Metric
- Within-ts rank-IC of the reversal signal (= −rank(ret_5m)) vs fwd_60m, on the **liquid tier only**, NW t.
- Net-of-cost L/S sharpe and **breakeven one-way bps** at the realized 60m-hold turnover.
- Compare breakeven vs the modeller's MEASURED liquid-tier half-spread curve (quote_agg_1m, from task #5 / research.common_spread_at_cadence), NOT a flat 2bps.

## Falsifier (what kills H1)
- If net-of-cost sharpe ≤ 0 / breakeven < measured liquid half-spread for the tradeable liquid set → H1 FALSE; reversal is real but uneconomic even at 60m hold. (This is the ~75% expected outcome — a clean documented negative, which is itself a result: it closes "could a lower-turnover reversal escape #5's cost wall?" with evidence, not assumption.)
- If the shuffle canary ≥ |IC| → the apparent reversal IC is overfit/leakage floor, discard regardless of net.

## Gates (all required — Lead rejects if missing)
1. **Shuffle-label canary** (permute labels within ts; |IC| must exceed canary).
2. **Survivorship neutralization** (per-symbol demean the predictions, re-backtest; reversal is a TIMING signal so it should SURVIVE demean — if it collapses, it was persistent per-symbol drift, not reversal).
3. **Net-of-MEASURED-cost** (per-name liquid half-spread, not flat) — H1's whole point is the cost wall.
4. **Turnover honesty** — report realized turnover at the 60m hold; do NOT assume the 2× reduction, measure it (a 60m hold with the native cadence may not halve turnover if positions churn).

## OUT-OF-SAMPLE split (binding — the hypothesis was observed on the full panel)
The reversal sign/persistence was observed pooled over 2024-01..2026-06. To avoid confirming on the same data:
- **TRAIN/observe window:** 2024-01-02 .. 2025-06-30 (where the effect was characterized).
- **TEST/OOS window:** 2025-07-01 .. 2026-06-11 (held out; the net-of-cost verdict is read ONLY here).
- The monthly table shows the effect is present in BOTH halves, so OOS is a fair test of stability, not a coin flip. Report IC + net separately for each window; H1 is judged on the OOS half.

## REGIME ARM (added wake-1 batch-2, from journal OBS6)
A second, cheap arm: condition the reversal on the daily cross-sectional dispersion regime. Full-panel
finding — reversal mean IC is monotone in calm: q1(calmest) -0.0275 → q5(most volatile) -0.0150. So:
- **Arm B:** same liquid × 60m-hold reversal, but EXCLUDE (or down-weight) the top-dispersion quintile of
  days (a calm-regime filter). The dispersion regime MUST be computed POINT-IN-TIME — prior session's
  realized cross-sectional vol, never the contemporaneous day's — or it's lookahead.
- **H2:** the calm-filtered reversal has a HIGHER / more stable net-of-cost breakeven than unfiltered,
  because it drops the high-variance trend/snapback days where the reversal is unreliable.
- **Falsifier:** if the filter does NOT improve net sharpe/breakeven on the OOS half, the regime structure
  is in-sample noise; report and drop.
This arm is what makes the proposal more than a re-test of #5 — it adds a regime conditioner #5 never had.

## Implementation note
Reuses experiments/battery.py machinery: a single-feature "signal = −ret_5m" predictor (no training needed, or a 1-feature LGBM), restricted to the liquid tier (ntile-4 by ADV from bars_1m), fwd_60m label, with the measured-cost backtest the modeller built for #5. No new data, no service change. Cheap — one of the 2-4/day long-shot slots.

## Disposition (Lead fills this in)
_pending_

## LEAD DISPOSITION — APPROVED (high value, runnable NOW), 2026-06-12
This is the most rigorous proposal in the batch — and it directly refines my task #5 verdict. APPROVED.
Validated: all 4 gates present; mechanism is the reversal STRUCTURE my #5 multivariate liquid model
blurred (LightGBM mixed reversal with continuation -> the +0.009 liquid IC hid a -0.020 univariate
reversal); the OUT-OF-SAMPLE split (2024-01..2025-06 observe / 2025-07..2026-06 test) is exactly right
and BINDING since the effect was observed in-sample — judge H1 on the OOS half only. Cost-gates against
research.common_spreads_at_cadence (now the canonical name — note the rename from _spread_). Your ~25%
confidence + "expect a documented honest no" framing is the right prior and the right attitude: a clean
OOS net-negative CLOSES "could a lower-turnover reversal escape #5's cost wall?" with evidence.

ONE GATE I'M STRENGTHENING: the persistence-to-60m (58% retained) is the load-bearing claim that this
isn't bid-ask bounce. Report the half-life explicitly and confirm the reversal sign is STABLE across the
OOS sub-period months (not driven by a few outlier days) — explorer-data outlier-day analysis applies to
your own signal too. Build the standalone (single-feature signal=-ret_5m, liquid ntile-4, fwd_60m,
measured-cost backtest, OOS split). This counts toward the data lens's >=3 for the Monday bar.
VERDICT is mine once it runs; interpretation is yours. Global exp count tracked.
