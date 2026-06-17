# W2 — Verdict

## DECISION: KILL

Decisive criterion (pre-registered): LIQUID OOS signed L/S net-of-cost per-trade bootstrap 95% CI lower bound > 0.

- Horizons where LIQUID OOS net-of-cost bootstrap CI excludes zero (lo>0): **NONE**
  - H=1d: net=-0.226% CI=[-0.358, -0.095] n=3830 | day-clustered t=-0.22
  - H=3d: net=-0.329% CI=[-0.534, -0.119] n=3822 | day-clustered t=-2.08
  - H=5d: net=-0.237% CI=[-0.530, +0.039] n=3811 | day-clustered t=-1.98
  - H=10d: net=-0.838% CI=[-1.944, -0.061] n=3773 | day-clustered t=-1.52
  - H=20d: net=-0.408% CI=[-1.086, +0.208] n=3665 | day-clustered t=-0.57
  - H=40d: net=+1.615% CI=[-1.346, +6.372] n=2115 | day-clustered t=-0.1

## Why KILL (the picture across tiers)
- The LIQUID-tier signed-by-reaction L/S is **net-NEGATIVE or zero-straddling at every horizon** (1d -0.23%, 3d -0.33%, 5d -0.24%, 10d -0.84%, 20d -0.41%; 40d +1.6% but t≈0, CI [-1.3,+6.4], n only 2115 and truncated by the data end). NO horizon clears the pre-registered gate (bootstrap CI lo > 0). At 2× cost it is strictly worse.
- The negative signs mean liquid earnings names tend to **mean-REVERT after the immediate reaction** (post-reaction reversal), the OPPOSITE of PEAD continuation, once the 13.4 bps round-trip cost is paid. The day-clustered t-stats at 3d/5d (~-2.0) point to a small *reversal*, not drift.
- The generic (un-signed) headline drift is ~0 at short horizons and only weakly +0.8% at 10d (t=1.06, inside the shuffle-canary p95 of +0.80%) — i.e. not distinguishable from the label-shuffle null. So even the "earnings names drift up on average" effect is not real here; and crucially the reaction-SIGN does not select the up-drifters (the short leg drifts up too), so an L/S cannot harvest it.
- Context tiers tell the same story: full-universe and mid signed L/S are net-negative; illiquid is net-negative at 1d (-4.3%) and noise elsewhere (n~980, CIs ±8%); megacap is pure noise (n~150). NO tradeable tier shows positive item-2.02 PEAD continuation net of cost. This REPLICATES the cycle-1/H10b lesson — no event drift lives in tradeable names — now specifically for the canonical earnings (item-2.02) subset that was never cleanly isolated before.

## Caveats (pre-flagged)
- **Reaction-sign proxy, not a true SUE.** Without a consensus-estimate feed the cohort is signed by the D+1 reaction, which is noisier than a genuine earnings surprise and can conflate the reaction with the drift. A clean SUE = an **estimates-feed DATA ASK**; a true SUE could in principle separate from this reaction-sign null, but the prior (PEAD arbitraged in large-caps) and this result both point the same way.
- **PEAD is heavily arbitraged in large-caps** and documented to survive mostly in small-caps — a liquid-dead result is the *expected* null (the H10b illiquid trap). The reversal sign suggests liquid earnings reactions if anything OVER-shoot and partially retrace.
- **No real train/OOS event separation.** The bar panel spans 2024-12-11..2026-06-16 (378 days), but the `filings` table only has 8-Ks from 2025-12-15 onward, so ALL 7,344 earnings events fall in the OOS half — TRAIN has 0 events (the n=0 TRAIN rows). The walk-forward split therefore does not provide an independent in-sample fit; the result rests on the demean + shuffle-canary + per-trade bootstrap on the full ~6-month event set (~3,830 liquid trades), which is a strong sample but a single ~2-quarter regime. A longer EDGAR backfill would let a genuine walk-forward run.
- 40d horizon is truncated for the most recent events (n drops to 2115), inflating its CI; it is not evidence of late drift.
