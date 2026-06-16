# W1 — Cross-sectional factor/price momentum, LIQUID portfolio L/S (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L2 (cross-sectional portfolio). The friction-wall design:
**portfolio diversification** — a tiny per-name edge that died single-name in cycles 1-2 can survive at
PORTFOLIO scale because idiosyncratic noise nets out across many names; combined with **low turnover**
(weekly/monthly rebalance) and the **LIQUID** universe (0.4-3 bps spread) so cost is small per rebalance.

## Hypothesis

A cross-sectional momentum decile long/short on a LIQUID universe — long the top-decile, short the
bottom-decile trailing-return names — earns a positive net-of-cost portfolio return at WEEKLY/MONTHLY
rebalance, surviving per-symbol demean + a per-rebalance bootstrap, in the liquid tier. (Cross-sectional
momentum is the canonical Jegadeesh-Titman factor; the open question for US large-caps in our 126-day window
is whether the LIQUID-tier L/S clears cost at low turnover — and we have honestly NOT tested a portfolio L/S
at all, only single-name signals.)

## Universe + data
- Bars 126 days (2025-12-15→2026-06-16). LIQUID universe = top ~500 by median daily dollar-volume (and a
  top-100 megacap sub-test). Daily close (last RTH bar) per name.
- Caveat (honest, pre-committed): 126 days is SHORT for a 3-12 month momentum formation window. So test BOTH
  (a) classic formation windows truncated to what fits (e.g. 21/42/63-day formation, skip the last 1-2 days,
  hold 5/10/21 days), AND (b) note that a clean 12-1 momentum needs more history → flag as a depth ask if
  the short-window version is promising. Survivorship: current-universe only — per-symbol demean is the
  control; flag that delisted names are absent.

## Test design
- Formation: trailing return over F ∈ {21, 42, 63} days, skip the most recent S ∈ {0, 2} days (the
  short-term-reversal skip). Rank cross-sectionally each rebalance date.
- Portfolio: decile (or top/bottom quintile) L/S, EQUAL-weighted, rebalanced every H ∈ {5, 10, 21} days.
  Report the PORTFOLIO return series (one number per rebalance period), not per-name IC.
- Cost: charge the measured per-name round-trip spread on each name that changes leg at each rebalance
  (turnover × measured spread); also a 2× stress. The portfolio L/S net = gross − cost.
- GATES: shuffle-canary (permute the formation-return → forward-return mapping within each rebalance
  cross-section); per-symbol demean (subtract each name's mean forward return); walk-forward OOS (first half
  TRAIN / last half OOS by date); per-REBALANCE bootstrap (resample the rebalance-period net returns, 95% CI
  must exclude zero ABOVE); day/period-clustered t.
- DECISIVE number: the OOS portfolio net-of-cost L/S return, per-rebalance bootstrap CI, at the measured
  spread, in the LIQUID universe.

## Expected / confidence
- Confidence the LIQUID-tier momentum L/S clears net-of-cost OOS with the bootstrap CI > 0: **~30%.**
  Cross-sectional momentum is real and portfolio-diversified by construction (the friction-wall-favorable
  shape), BUT (a) it's heavily arbitraged in US large-caps (McLean-Pontiff decay), (b) 126 days is thin for
  the formation window, (c) momentum suffers crashes. Pre-commit the modest prior.
- KEEP-AS-LEAD: OOS portfolio net positive, bootstrap CI > 0, demean+canary survived, low turnover → a
  portfolio L/S paper container + deepen the history. AMBIGUOUS: positive but CI straddles / thin window.
  KILL: OOS net ≤ 0 or inside canary.

## Friction-wall scorecard
[portfolio-diversified ✓] [low-turnover ✓ weekly/monthly] [liquid ✓ top-500] — this is the archetype of the
shape the program is designed to find. If the canonical portfolio factor doesn't clear cost in liquid names,
that itself is a sharp finding (the wall binds even on the diversified shape).
