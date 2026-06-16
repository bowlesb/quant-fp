# Verdict — H2: OFI marginal lift over vwap_dev

## Against the pre-registered EXPECTED
- **Primary expectation (conf ~40%): "OFI carries SOME standalone short-horizon IC, POSITIVE/continuation sign,
  with a clean canary."** → **CONFIRMED, in the rolling-flow windows.** `ofi_15` IC=+0.0185 (t=+3.96) and
  `ofi_15_norm` IC=+0.014 (t=+3.0) at H=15 sit clearly outside the 10-seed shuffle canary band (±~0.007). Sign
  is POSITIVE = continuation, exactly as pre-committed (Cont–Kukanov–Stoikov). The 1-minute `signed_vol_z` is
  noise (IC≈0). So "OFI has standalone signal" is TRUE, but it is carried by the 15-min flow window, NOT by the
  composite I scored (which the dead `signed_vol_z` member dilutes below the gate).
- **Secondary / load-bearing (conf ~30%): "OFI's MARGINAL lift over vwap_dev is positive and beats the canary
  band."** → **NOT ESTABLISHED (AMBIGUOUS).** On this 3-day, megacap-excluded panel the vwap_dev baseline is
  itself near-zero/inside-canary (low power), and vwap_dev (sign −) and OFI (sign +) partially cancel in the raw
  sum, so the combined `+OFI` ≈ 0. The additive-vs-conditioner question (which I flagged as genuinely uncertain)
  is unresolved at this scale.
- **Pre-committed falsifier:** KILL required "OFI-only ≤ canary AND +OFI ≤ baseline beyond canary." OFI-only
  *composite* is inside canary, but its best member (`ofi_15`) clearly clears canary, so the strict KILL is NOT
  triggered.

## Is OFI an additive carrier, a conditioner, or absent?
On this panel: **a real standalone carrier in the 15-min signed-flow window (positive/continuation), of small
magnitude (IC ~0.015–0.019, t~3–4)** — i.e. NOT absent. Whether it is *additive on top of* vwap_dev or merely an
*interaction/conditioner* (the high-vwap-dev-but-still-bought name that won't revert) is NOT decidable here
because vwap_dev didn't fire at strength on 3 days. The opposite-sign cancellation I pre-warned about is exactly
what showed up in the raw sum.

## Decision: **AMBIGUOUS — H2 leans KEEP-FOR-RETEST, not yet a PR.**
OFI clears the standalone canary gate with a sane continuation sign and a respectable t (~4), which is more than
a null. But (a) the *load-bearing* marginal-lift-over-vwap_dev claim is unproven because the baseline was
under-powered, (b) the composite as constructed fails the gate, and (c) net-of-cost is negative at minute
rebalancing. Not enough to open an OFI-feature PR (per the rules, PR only if H2 cleanly clears the marginal gate).

## One-line next step
Re-run at **full universe (incl. megacaps) × ≥15 days** with an **orthogonalized** test: residualize the
forward return on vwap_dev first, then measure `ofi_15` / `ofi_15_norm` IC on the residual (drop the dead
`signed_vol_z`), and pair with a horizon-matched (15–30 min) holding so net-of-cost is meaningful — that
directly answers additive-carrier vs conditioner.
