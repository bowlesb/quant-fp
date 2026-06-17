# W13 — SECTOR / industry momentum via sector ETFs, LIQUID (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L2/L1 survey #3 (Moskowitz–Grinblatt 1999 — industries
trend). Friction-wall design: trade via SECTOR ETFs (the LOWEST-friction liquid instruments — SPY/XLK/XLF
spreads ~0.1–0.4 bps, unlimited capacity), LOW turnover (monthly), PORTFOLIO (a basket of sector legs). The
cheapest-to-trade momentum form, sidestepping the single-name momentum that died (W1) and the per-name spread.

## Hypothesis

The 11 GICS sector ETFs (XLK/XLF/XLE/XLV/XLI/XLP/XLY/XLU/XLB/XLRE/XLC) exhibit cross-sectional momentum: the
top trailing-return sectors outperform the bottom over the next month. A long-top / short-bottom sector-ETF
portfolio earns a positive net-of-cost return at monthly rebalance — and because sector ETFs are the most
liquid, lowest-spread instruments, the friction wall is at its weakest here.

## Universe + data
- Sector ETFs: the 11 SPDR sector ETFs + SPY (market). Daily bars (need the incoming ≥18-month history — a
  6-month formation × monthly rebalance needs depth; pairs with the data ask). If the ETFs aren't in
  `/store/raw/bars`, fetch them via Alpaca historical (they're top-liquid; small fetch) and note it.
- Formation: trailing return over F ∈ {21, 63, 126} days (1/3/6 months). Rank the 11 sectors each month.

## Test design
- Each month: long the top-3 (or top-tertile) momentum sectors, short the bottom-3, equal-weight. Hold 21
  days. Build the per-rebalance non-overlapping portfolio net return series.
- Cost: sector-ETF round-trip spread (~0.1–0.4 bps, measure from quotes if available — negligible) × the
  monthly turnover. The friction is trivial here by design.
- ALSO test the ABSOLUTE / time-series form (long sectors with positive trailing return, short negative) as a
  trend overlay, and a sector-NEUTRAL single-name momentum (does ranking within-sector beat the W1
  cross-sectional momentum that died on a level artifact?).
- GATES: shuffle-canary (permute sector→forward-return — though with only 11 sectors the canary is coarse,
  so ALSO a block-bootstrap of the monthly return series); per-rebalance bootstrap (CI > 0); walk-forward
  OOS; cost. n is small (11 sectors, ~6–18 monthly rebalances) — power is the honest constraint; report it.
- DECISIVE: OOS sector-momentum portfolio net-of-cost, per-rebalance bootstrap / block-bootstrap CI > 0.

## Expected / confidence
- Confidence sector momentum clears net-of-cost OOS with CI > 0: **~30%** — friction-favorable (ETF spreads
  trivial, capacity unlimited) and documented, BUT (a) only 11 instruments (low cross-sectional breadth → wide
  CIs), (b) ~6–18 monthly rebalances even on 18-month bars (time-underpowered), (c) sector momentum has
  decayed somewhat post-publication. The low-friction shape is the appeal; the small-n is the risk. Pre-commit
  the prior.
- KEEP-AS-LEAD: OOS sector-momentum net positive, bootstrap/block CI > 0 → a sector-rotation ETF paper
  container (clean, low-turnover, liquid). AMBIGUOUS / "needs multi-year": directionally positive but
  small-n CI straddles. KILL: no momentum beyond canary OR net ≤ 0.

## Friction-wall scorecard
[lowest-friction ✓✓ sector ETFs ~0.1-0.4 bps] [unlimited capacity ✓✓] [low-turnover ✓ monthly] [portfolio ✓
basket] — the single most friction-FAVORABLE instrument in the whole program. If momentum pays ANYWHERE
net-of-cost, it should be here. The honest risk is statistical power (only 11 instruments), not friction.
Dispatch after the ≥18-month bars land.
