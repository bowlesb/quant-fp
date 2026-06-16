# Leads — Modelling Agent (MA), append-only, single-writer

Ranked promising leads with evidence + proposed next action. A lead enters here only with REAL numbers
from a pre-registered test. Empty leads / hunches stay in `BACKLOG.md`, not here.

_(Standing position from `docs/EXPERIMENTS.md`: NO price-only edge has cleared the 4-gate battery; the
strongest historical candidate, the conditional gap-fade, was KILLED by the tradeable-entry trap. The first
NON-price lead to clear a standalone canary on real multi-day data is **H2 OFI** (below) — a keep-for-retest,
NOT yet edge.)_

| date | lead | evidence (real) | net-of-cost | next action |
|---|---|---|---|---|
| 2026-06-15 | **H2 OFI 15-min signed-flow** — KEEP-FOR-RETEST (first standalone signal past canary on real multi-day data) | self-built Alpaca panel (80 liquid names × 3 days, 89,819 rows, tick-rule OFI): `ofi_15` rank-IC **+0.0185 (t +3.96)** / `ofi_15_norm` +0.014 (t +3.0) at H=15, **POSITIVE = continuation**, clearly outside the 10-seed canary (±~0.007); H=5 corroborates. `signed_vol_z` (1-min) is noise. vwap_dev baseline under-powered on 3 days, so the raw sum cancels (vwap_dev − vs OFI +). | NEGATIVE at minute rebalancing (~1.9bps cost > ≤1bps gross) — horizon mismatch, not a verdict | RETEST: full universe (incl megacaps) × ≥15 days, **orthogonalize fwd-ret on vwap_dev** then test `ofi_15`/`ofi_15_norm` on the residual, drop `signed_vol_z`, **horizon-matched 15–30min holding**. Resolves additive-carrier vs conditioner. |
| 2026-06-15 | ~~H1 vwap_dev cost-conditioning~~ KILLED (CONFIRMED at depth) | DEPTH recheck (629 names × 126 days, 26.7M sym-min): illiquid/liquid |IC| ratio **6.37× (H15) / 9.70× (H30)** — the illiquid-skew STRENGTHENS at depth, NOT a single-Monday artifact. liquid-tier IC only −0.017/−0.014. | liquid (only real-priced) tier FAILS cost: net **−5.4bps (H15) / −7.0bps (H30)** @8bps/period | H1 stays dead. The reversion does not live in a tradeable liquid subset. No re-open. |

## ★ Powered vwap_dev baseline (for the H2-RETEST) — 2026-06-16
- **Pooled vwap_dev rank-IC = −0.0581 (day-clustered t −32.3) H15 / −0.0657 (t −27.6) H30** over 629×126
  (`2026-06-16-vwap-baseline-depth/`), superseding the under-powered 3-day −0.0044. **BUT** the pooled number
  is illiquid-weighted (illiquid IC −0.111/−0.134 vs liquid −0.017/−0.014) and the illiquid tier carries a
  **forward-fill stale-close artifact** (untradeable t+1 entry). → The H2-RETEST should orthogonalize OFI
  against the **LIQUID-tier baseline (−0.017/−0.014)** — the only tradeable, real-priced tier — NOT the
  inflated pooled −0.058. OFI must add value in the LIQUID tier to matter economically.

## Down-ranked / killed (kept for the record)
- **H1 (vwap_dev cost-conditioning)** — killed 2026-06-15, CONFIRMED at depth 2026-06-16: the reversion
  carrier is overwhelmingly an ILLIQUID-name phenomenon (illiq/liq |IC| 6–10× at 126 days) and the liquid
  tier (the only tradeable one) fails cost net. Cost-conditioning is structurally doomed. Definitively dead.
