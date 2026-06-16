# W12 — Net share ISSUANCE / buyback drift, LIQUID portfolio (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L5 (EDGAR fundamentals CONTENT) / L1 survey #2
(Pontiff–Woodgate 2008; Fama–French 2008 — confirmed to work in BIG stocks, unlike most anomalies).
Friction-wall design: a LOW-TURNOVER (annual-ish), LIQUID, PORTFOLIO fundamental tilt — the cleanest
large-cap-robust anomaly the survey surfaced. Needs the incoming ≥18-month bar history to certify a slow
premium (pairs with the data ask).

## Hypothesis

Firms that REDUCE shares outstanding (buybacks) outperform; firms that ISSUE underperform — a cross-sectional
LIQUID portfolio long-low-issuance / short-high-issuance earns a positive net-of-cost premium at quarterly/
annual rebalance. (Net-share-issuance is one of the few anomalies documented to SURVIVE in large-caps —
Fama-French 2008 explicitly find it in big stocks — making it friction-favorable by construction.)

## Universe + data
- Shares-outstanding per name over time: from EDGAR XBRL companyfacts
  (`data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` → `dei:EntityCommonStockSharesOutstanding` and/or
  `us-gaap:CommonStockSharesOutstanding`/`WeightedAverageNumberOfSharesOutstandingBasic`) — one call per CIK,
  point-in-time (use the filing's `end`/`filed` dates, look-ahead-safe: only use a shares value once its
  filing was publicly available). ALSO cross-check against `corporate_actions` (buyback/split events).
- Net issuance = log(shares_t / shares_{t−1y}) — the trailing 1-year change in split-adjusted shares.
- LIQUID universe = top ~500 by dollar-volume. Bars (≥18-month incoming) for forward returns.

## Test design
- Rank liquid names by net issuance (trailing 1y, split-adjusted). Quintile L/S: long the bottom (buyback /
  shrinking-share) quintile, short the top (issuing) quintile. Equal-weight. Rebalance quarterly (when new
  10-Q/10-K shares land) — VERY low turnover.
- Forward holding: 1–3 months (the premium is slow). Build the per-rebalance non-overlapping portfolio net
  return series.
- GATES: shuffle-canary (permute issuance→forward-return); per-symbol demean; walk-forward OOS; per-rebalance
  bootstrap (CI excludes zero above); cost at measured liquid spread (negligible at quarterly turnover) + 2×.
  DECISIVE: OOS LIQUID portfolio net-of-cost, per-rebalance bootstrap CI > 0.
- POWER NOTE: even with 18-month bars there are only ~6 quarterly rebalances — honest about n; the
  cross-sectional breadth (500 names × the issuance spread) is the power, not the time dimension. If
  underpowered, "promising, needs multi-year" rather than a confident verdict.

## Expected / confidence
- Confidence the LIQUID net-issuance L/S clears net-of-cost OOS with bootstrap CI > 0: **~35%** — among the
  top priors because (a) it's explicitly documented to work in BIG stocks (Fama-French), (b) very low
  turnover (friction-trivial), (c) it's a real fundamental, not a price pattern. Risks: the shares-outstanding
  XBRL field is messy (multiple tags, restatements, split-adjustment care needed); only ~6 quarterly
  rebalances on 18-month bars (cross-sectional power, not time). Pre-commit the prior.
- KEEP-AS-LEAD: LIQUID OOS net positive, bootstrap CI > 0, demean+canary survived → a low-turnover
  fundamental paper container + multi-year certification. AMBIGUOUS / "needs multi-year": cross-sectionally
  positive but time-underpowered. KILL: no spread beyond canary OR net ≤ 0.

## Friction-wall scorecard
[fundamental info ✓ not price] [low-turnover ✓✓ quarterly] [liquid ✓ documented in big stocks] [portfolio ✓]
— the cleanest, most large-cap-robust fundamental tilt available, and the issuance data is in our 3.2M-filing
EDGAR corpus (XBRL). Dispatch after the ≥18-month bars land (for the forward-return depth).
