# W13 — Sector momentum via the 11 SPDR sector ETFs — VERDICT

## KILL

Pre-registered prior was ~30% to clear net-of-cost OOS with CI > 0. It did not — and not because of
friction or small-n ambiguity, but because **the sector-momentum signal is the wrong sign over this window**.

### Why KILL (vs AMBIGUOUS)
- The decisive gate was: **OOS sector-momentum net-of-cost with bootstrap/block CI > 0.** Every formation
  window (F=21/63/126), in **both** the cross-sectional (top-3/bottom-3) and time-series (sign-of-trailing)
  forms, in-sample **and** OOS, produced a **negative** net return. Not a single positive cell.
- This is the friction-FAVORABLE end of the program (cost ≤0.63 bps, ~3 orders of magnitude below the
  signal). If momentum were real but expensive, we'd see positive gross killed by cost. Instead **gross
  itself is negative** — sectors mildly **reverse** at the monthly horizon over 2024-12 → 2026-06.
- Several CIs exclude zero **below** (F=126 cross-sectional block CI [-500, -37]; F=21 time-series boot CI
  [-611, -113]; F=126 OOS both forms). The shuffle canary confirms the ranking carries no positive
  information (p = 0.27–0.39 with negative real means).

### Honest power caveat
n_rebalances = 11–16 across only 11 instruments → CIs are genuinely wide. A single 18-month window cannot
rule out that sector momentum works in *other* regimes (it is a published long-horizon effect). But the
deliverable's bar is OOS net > 0 with CI > 0, and the realized estimate is uniformly and materially
**negative** with no positive form anywhere — this is a wrong-signed result, not a "needs more data"
straddle. Re-test only if a multi-year (5y+) sector-ETF panel lands; do not build a paper container on this.

### One-line
KILL — sector-ETF momentum is negative (mild reversal) net-of-trivial-cost across all formation windows and
both forms, IS and OOS; the friction wall was never the binding constraint, the signal simply isn't there.
