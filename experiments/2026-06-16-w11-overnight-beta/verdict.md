# W11 — Overnight-BETA premium: verdict

## VERDICT: KEEP-AS-LEAD (directionally suggestive) — certify on ≥18-month bars

The pre-registered DECISIVE criterion was: **OOS overnight high-low-beta L/S net-of-cost bootstrap CI > 0
AND the overnight>intraday split in the predicted direction.** On the available 126d window:

- **Split: PASSES, cleanly and consistently.** High-minus-low-beta L/S is **+75 bps/day OVERNIGHT** vs
  **−23 bps/day INTRADAY** — the exact Hendershott–Livdan–Rösch sign pattern, holding in **3/3 rebalances**
  (overnight positive every time, intraday negative 2/3). This is the sharp, falsifiable prediction and it is
  the part of the test with the most signal.
- **Net-of-cost overnight: positive and cheap.** +72.6 bps/day net (3 bps/side), +70.1 under 2× stress;
  turnover only 12.8%/rebalance. Cost is negligible — the friction-favorable, low-turnover profile claimed.
- **Canary: PASSES.** Permuting beta collapses overnight L/S from +75 to +9 bps with a CI that straddles
  zero (~8× smaller) — the signal lives in the beta sort.
- **Robustness: PASSES.** Survives ±15% winsorization and median-of-leg (a few crypto-miner gappers do not
  drive it; it is a broad high-beta-leg overnight tilt).

**Why KEEP-AS-LEAD and not a confident KEEP:**
1. **Power.** Only **3 non-overlapping rebalances** after the 60d beta warmup on 126 days. The per-rebalance
   bootstrap resamples 3 points and the IS/OOS split is 1-vs-2 — neither is a real statistical OOS. The CI
   "excludes zero" mechanically but cannot be trusted as decisive. The **split direction**, not the CI, is the
   load-bearing evidence.
2. **Regime confound.** In this 126d window "high-beta" ≈ a crypto-miner / quantum / nuclear / AI-speculation
   cohort that mechanically gaps at the U.S. open (crypto trades 24h). The overnight premium may be the durable
   beta-risk premium OR a regime-specific overnight-gap factor — **inseparable on 126 days**. A longer history
   spanning multiple regimes is required to tell them apart.
3. **MOO/MOC auction realism.** The bet executes at the close/open auctions; the 3 bps quote-spread proxy is
   not measured auction slippage. Certification must use real auction fills.

## Recommendation
- **Promote to a LEAD** (not a deployable strategy yet): the sign, the canary, the low turnover, and the
  robustness all line up with the literature's #1 pick — the most friction-favorable, structurally-durable
  candidate surfaced. NOT a re-run of the killed W4 level (this is the conditional beta sort × the split).
- **Data ask to certify:** a **≥18-month** (ideally multi-year, multi-regime) bar history so beta is stable,
  the rebalances number in the dozens (real bootstrap + walk-forward OOS), and the beta-risk-premium vs
  crypto-overnight-gap confound can be disentangled (e.g., beta-residualize against a crypto/overnight-gap
  factor; control for the speculative cohort). Add measured close/open auction slippage to the cost model.

**One-line:** Predicted overnight≫intraday beta split is present, consistent (3/3), canary-clean, robust, and
net-of-cost positive — but on only ~3 rebalances and a single speculative-cohort regime, so **directionally
suggestive, certify on ≥18-month bars** rather than a confident KEEP.
