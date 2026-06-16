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
| 2026-06-15 | ~~H1 vwap_dev cost-conditioning~~ KILLED | live-session probe: vwap_dev→fwd-ret rank-IC −0.048 (t−5.1) H5 / −0.028 (t−2.7) H15, clean canary; BUT reversion **stronger in illiquid half** (illiq/liq |IC| ratio 2.06× H5, 4.01× H15) — exceeds the pre-committed 2× falsifier | n/a (liquidity-gating would gate AWAY the signal) | DOWN-RANK H1. Carrier lives where cost is worst; cost-conditioning can't rescue economics. Confirm on a multi-day panel when it exists. |

## Down-ranked / killed (kept for the record)
- **H1 (vwap_dev cost-conditioning)** — killed 2026-06-15 on a single live session: the proven reversion
  carrier is concentrated in ILLIQUID names (the high-cost tier), so the cost-conditioning thesis is
  structurally doomed. One Monday session is noisy, but the illiquid-stronger sign is the pre-registered
  down-rank trigger and is consistent across two horizons with a clean canary. Re-confirm on 5–10 days.
