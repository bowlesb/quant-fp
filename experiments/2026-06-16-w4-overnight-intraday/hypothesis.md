# W4 — Overnight vs intraday return decomposition, LIQUID portfolio (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L7 (overnight/seasonality). Friction-wall design: a
PORTFOLIO premium captured with LOW turnover (one decision per day per leg) in LIQUID names. Directly
re-tests, cleanly, the thing cycle-0 found was survivorship — but as a portfolio + demeaned design.

## Hypothesis

Decomposing each liquid name's daily return into OVERNIGHT (prev-close → today-open) and INTRADAY
(today-open → today-close), one component carries a persistent, PORTFOLIO-DIVERSIFIABLE premium that
survives per-symbol demean (i.e. it is NOT just survivorship/level) and clears cost at low turnover.
Documented (Lou-Polk-Skouras "overnight returns persist"; Hendershott et al; the "overnight anomaly").

## Universe + data
- Bars 126 days. LIQUID universe = top ~500 by dollar-volume (+ top-100 megacap sub-test). Per name per day:
  open (first RTH bar open ≥09:30 ET = 13:30 UTC — UTC-correct, RESEARCH_PITFALLS #1), prev close, today
  close. Overnight = open/prev_close − 1; intraday = close/open − 1.

## Test design
1. **Descriptive:** mean overnight vs mean intraday return per name, pooled and cross-sectional; is one
   component systematically positive/negative across the LIQUID universe?
2. **Survivorship control (the load-bearing test):** the raw "overnight is positive" effect is dominated by
   survivorship (winners survive). PER-SYMBOL DEMEAN each component (subtract the name's own mean overnight /
   own mean intraday) — does ANY tradeable structure remain after removing the level? Cycle-0's overnight
   "edge" COLLAPSED under demean; this is the honest re-test.
3. **Cross-sectional L/S (the portfolio form):** does a name's RECENT overnight (or intraday) return predict
   its NEXT overnight (or intraday) return cross-sectionally — i.e. an overnight-momentum or
   overnight-reversal portfolio L/S? Decile L/S, daily rebalance on the relevant component, equal-weight.
4. **The tradeable entry:** an overnight bet = buy at today's close, sell at tomorrow's open (or the MOC/MOO
   auctions). An intraday bet = buy at open, sell at close. Charge the measured spread on each leg; note the
   auction-fill caveat (MOC/MOO get the auction price — model it as the open/close print + a stress).
- GATES: canary, per-symbol demean (PRIMARY here — it's the whole question), walk-forward OOS, per-trade
  bootstrap on the realized overnight/intraday round-trips, cost gate at measured spread + 2×.

## Expected / confidence
- Confidence a demean-surviving, cost-clearing overnight/intraday PORTFOLIO effect exists OOS in liquid
  names: **~25%.** The raw effect is strong but mostly survivorship/level; the literature's tradeable
  cross-sectional form (overnight-return persistence as a cross-sectional signal) is more credible but
  arbitraged. Low turnover + portfolio shape is friction-favorable. Pre-commit the prior.
- KEEP-AS-LEAD: a demean-surviving component or cross-sectional L/S nets positive OOS, bootstrap CI > 0,
  measured cost — then a low-turnover paper container. AMBIGUOUS: survives demean but cost-marginal. KILL:
  collapses under demean (survivorship, like cycle-0) OR net ≤ 0 OOS.

## Friction-wall scorecard
[low-turnover ✓ one bet/day] [portfolio ✓ cross-sectional L/S form] [liquid ✓] — and the per-symbol-demean is
the PRIMARY gate, not an afterthought, because this exact family already fooled us once via survivorship.
