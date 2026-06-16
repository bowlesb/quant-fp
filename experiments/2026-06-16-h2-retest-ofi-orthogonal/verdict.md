# H2-RETEST — verdict: **KILL**

## Against the pre-registration

The pre-registered KILL fires if ANY of: (1) standalone ofi_15 |t| < 2.0 at H15; (2) marginal residual IC
|t| < 2.0 at BOTH H15 and H30; (3) canary band overlaps the marginal IC; (4) decile L/S gross < measured
spread. **ALL FOUR kill conditions are met:**

1. **Standalone ofi_15 |t| = 0.38 at H15** (« 2.0). Every OFI/sv signal is |t| < 1 and sits INSIDE its
   shuffle-canary band. (Expected 0.012–0.020 / |t| ≥ 2.5 — did NOT replicate the 3-day t=3.96.)
2. **Marginal-over-vwap_dev |t| ≤ 1.45 everywhere** (best = sv_15_norm +1.45 @ H15; OFI ≤ 0.89), below the
   2.0 bar at BOTH horizons.
3. Canary bands (±~0.002–0.003) overlap every OFI marginal IC.
4. Decile L/S gross 0.35–0.86 bps « 6.41 bps round-trip cost (~8× short).

## Is OFI an additive carrier, a conditioner, or absent?

On the powered, megacap-inclusive liquid panel (250×20): **ABSENT** as a standalone or additive
cross-sectional ranker. It carries no IC beyond canary and no marginal lift over vwap_dev that clears
significance or cost. The prior 3-day signal was a small-sample / smaller-cap artifact that did not survive
the powered test. The ONLY signal that clears canary remains `vwap_dev_15` (reversion, t −2.76) — and it
itself is uneconomic (the standing verdict).

## Decision: **KILL** OFI / signed-flow as a cross-sectional feature for the liquid tier.

- The `order_flow_imbalance` feature spec (Case-B, the parity-cornerstone change) is **NOT promoted to a
  feature PR** — there is no edge to justify touching the live capture path for it. Shelved.
- `signed_trade_ratio` (Case-A) is a different decision: it SHIPPED on parity + correctness (a clean
  universe-wide primitive), NOT on this edge result — and this result confirms it is an input, not a
  signal. That stands; no contradiction.

## This is a good result (honest null)

We powered the test the prior cycle flagged as ambiguous and got a clean answer: OFI does NOT add tradeable
marginal lift over vwap_dev in the current liquid regime. That closes the H2 lead and de-risks chasing a
small-sample mirage. One-line next step: **H3** (book-DEPTH/spread as a vwap_dev CONDITIONER, not a ranker;
pre-registered in `experiments/2026-06-16-h3-depth-conditioner/`) — the one remaining quote-microstructure
angle, reusing this exact panel + the per-minute spread/imbalance/depth columns.

## Caveats (honest)

- 20 days is still a MONTH, not a regime sweep — OFI could matter in a higher-vol regime; this kills it for
  the CURRENT calm-tape liquid cross-section, not for all time.
- Forward returns are entered at minute-T close (IC study); a strict tradeable-entry (≥09:35, open-spread
  cost) would only LOWER the net, so the KILL is conservative-safe.
