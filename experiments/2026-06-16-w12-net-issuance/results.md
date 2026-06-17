# W12 — Net share issuance / buyback L/S (LIQUID) — Results

## Coverage
- Liquid universe: top 500 by median daily dollar-volume (min ≈ $139M/day).
- 489/500 mapped to CIK (11 unmapped = sector-SPDR ETFs, correctly excluded).
- **482 names with usable XBRL shares-outstanding.** Avg **458 names/rebalance** carry a finite trailing-1y
  net-issuance signal.
- **5 non-overlapping quarterly rebalances** (HOLD=63 trading days) on the 378-day bar window
  (2024-12-11 → 2026-06-16). Time-underpowered by construction; cross-sectional breadth is the power.
- Tag mix per rebalance: ~85% `dei:EntityCommonStockSharesOutstanding` (clean instant measure), ~11%
  weighted-average-basic fallback, ~3% other. Consistent across all 5 rebalances.

## Per-rebalance L/S (long bottom-quintile = buyback, short top-quintile = issue)

| rebalance → exit            | n   | long ret | short ret | gross  | net    |
|-----------------------------|-----|----------|-----------|--------|--------|
| 2024-12-11 → 2025-03-17     | 454 | −3.70%   | −6.69%    | +2.99% | +2.89% |
| 2025-03-17 → 2025-06-16     | 455 | +4.27%   | +20.30%   | −16.03%| −16.13%|
| 2025-06-16 → 2025-09-16     | 461 | +18.71%  | +55.08%   | −36.37%| −36.47%|
| 2025-09-16 → 2025-12-15     | 462 | +6.06%   | −2.72%    | +8.78% | +8.68% |
| 2025-12-15 → 2026-03-18     | 460 | −0.64%   | −3.69%    | +3.05% | +2.95% |

- Long-leg mean issuance ≈ −0.055 (genuine buybacks/shrinking share counts); short-leg mean issuance ≈
  +0.17 to +0.25 (real dilution). The ranking IS separating buyback vs issue names cleanly.

## Aggregate (per-rebalance bootstrap, 10k)

| metric                       | mean    | 95% CI               | p(>0) |
|------------------------------|---------|----------------------|-------|
| GROSS                        | −7.52%  | [−24.17%, +5.33%]    | —     |
| NET (5 bps RT, both legs)    | −7.62%  | [−23.37%, +5.23%]    | 0.178 |
| NET @ 2× cost                | −7.72%  | [−23.47%, +5.13%]    | —     |
| per-symbol DEMEAN            | +2.89%  | [−16.21%, +18.21%]   | —     |
| **walk-forward OOS NET (n=3)**| **−8.28%** | **[−36.47%, +8.68%]** | —   |

- **Shuffle-canary** (20 seeds): mean +0.63%, abs-max 7.31% — essentially zero; **no look-ahead leakage**,
  but also the real signal's mean (−7.6%) is the WRONG sign and well inside the canary envelope.

## Why the sign is negative (regime confound)
The short leg (high issuers) is dominated by 2025's dilutive momentum WINNERS:
- 2025-06→09: OPEN +1498%, LCID +815%, ONDS +262%, WULF +152% (crypto-miners / EV / real-estate-tech).
- 2025-03→06: QUBT +165%, SMR +141%, LEU +122% (quantum / small-modular-nuclear).
- 2024-12→03: QBTS +157%, RGTI +50%, QUBT +25% (quantum).

In this 18-month window, "high share issuance" coincided with "hot, serially-dilutive spec-tech growth name"
— precisely the cohort that ripped hardest in the 2025 melt-up. Shorting them lost catastrophically. The
two melt-up rebalances (−16%, −36%) drive the entire negative mean; the other three are mildly positive
(+2.9%, +8.7%, +3.0%), hinting the documented effect may exist in calmer regimes — but with only 5
rebalances, two regime-dominated, nothing is statistically separable from zero or from the canary.

## Decisive criterion
Pre-registered DECISIVE = LIQUID OOS net-of-cost per-rebalance bootstrap **CI > 0**.
Observed OOS net CI = **[−36.47%, +8.68%]** → contains zero, point estimate negative → **does NOT clear.**
