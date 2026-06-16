# Leads — Modelling Agent (MA), append-only, single-writer

Ranked promising leads with evidence + proposed next action. A lead enters here only with REAL numbers
from a pre-registered test. Empty leads / hunches stay in `BACKLOG.md`, not here.

_(Standing position from `docs/EXPERIMENTS.md`: NO price-only edge has cleared the 4-gate battery; the
strongest historical candidate, the conditional gap-fade, was KILLED by the tradeable-entry trap. **UPDATE
2026-06-16: H2 OFI — the only NON-price lead to clear a 3-day canary — was KILLED on a powered 250-name ×
20-day panel (did not replicate; standalone |t|<1, orthogonalized marginal |t|≤1.45<2.0, cost gate fails
~8×). H3 (depth/spread conditioner) ALSO killed. NO price-only OR order-flow cross-sectional signal clears
the cost gate, and NO microstructure conditioner rescues vwap_dev. Next lever = HORIZON, H9.)**_

| date | lead | evidence (real) | net-of-cost | next action |
|---|---|---|---|---|
| 2026-06-16 | ~~**H2 OFI / signed-flow**~~ **KILLED at depth** (the 3-day signal did NOT replicate on a powered panel) | RETEST on the COMPLETE `/store/raw`: **250 liquid names (incl megacaps) × 20 days, 1.57M sym-min**, true CKS OFI from quotes (`2026-06-16-h2-retest-ofi-orthogonal/`). Standalone `ofi_15` rank-IC **−0.0017 (t −0.38)** at H15 — INSIDE the canary band; EVERY OFI/sv signal |t| < 1. Orthogonalized over vwap_dev: best marginal = `sv_15_norm` **t +1.45** (< the 2.0 bar); OFI marginal |t| ≤ 0.89. Only `vwap_dev_15` clears canary (IC −0.0233, t −2.76). | **FAILS**: decile L/S gross **0.35–0.86 bps « 6.41 bps** round-trip spread (~8× short). | KILLED, no re-open in this regime. `order_flow_imbalance` spec SHELVED. The clean `signed_trade_ratio` primitive still SHIPS (PR #33, parity+correctness, an input not a signal). |
| 2026-06-16 | ~~**H3 book depth/spread as a vwap_dev CONDITIONER**~~ **KILLED** | reuse the H2 panel (250×20). Flat vwap_dev H15 net **−10.45 bps** (gross 0.79, spread cost 11.24). Best cell = tight-spread tercile: SLASHES cost (2.70 vs 11.24) but gross there only **0.38 bps** (net −2.32), within **0.04 bps** of the canary max (−2.36) = noise. Depth + size-imbalance terciles add nothing. (`2026-06-16-h3-depth-conditioner/`). | FAILS — net negative beyond the canary in every cell. | KILLED. The microstructure-CONDITIONING branch (H1–H3) is CLOSED — conditioning lowers cost but can't manufacture signal. Next lever = HORIZON (H9). |
| 2026-06-15 | ~~H1 vwap_dev cost-conditioning~~ KILLED (CONFIRMED at depth) | DEPTH recheck (629 names × 126 days, 26.7M sym-min): illiquid/liquid |IC| ratio **6.37× (H15) / 9.70× (H30)** — the illiquid-skew STRENGTHENS at depth, NOT a single-Monday artifact. liquid-tier IC only −0.017/−0.014. | liquid (only real-priced) tier FAILS cost: net **−5.4bps (H15) / −7.0bps (H30)** @8bps/period | H1 stays dead. The reversion does not live in a tradeable liquid subset. No re-open. |

## ★ Powered vwap_dev baseline (for the H2-RETEST) — 2026-06-16
- **Pooled vwap_dev rank-IC = −0.0581 (day-clustered t −32.3) H15 / −0.0657 (t −27.6) H30** over 629×126
  (`2026-06-16-vwap-baseline-depth/`), superseding the under-powered 3-day −0.0044. **BUT** the pooled number
  is illiquid-weighted (illiquid IC −0.111/−0.134 vs liquid −0.017/−0.014) and the illiquid tier carries a
  **forward-fill stale-close artifact** (untradeable t+1 entry). → The H2-RETEST should orthogonalize OFI
  against the **LIQUID-tier baseline (−0.017/−0.014)** — the only tradeable, real-priced tier — NOT the
  inflated pooled −0.058. OFI must add value in the LIQUID tier to matter economically.

## Down-ranked / killed (kept for the record)
- **H3 (book depth/spread as a vwap_dev CONDITIONER)** — killed 2026-06-16. Tight-spread tercile slashes the
  cost wall (2.70 vs 11.24 bps) but vwap_dev gross there is only ~0.4 bps (net −2.32, within 0.04 bps of
  canary). Depth + size-imbalance add nothing. Closes the microstructure-CONDITIONING branch (H1–H3).
- **H2 (OFI / signed-flow marginal lift over vwap_dev)** — killed 2026-06-16 on the powered panel
  (250 liquid × 20 days, true CKS OFI). The 3-day t=3.96 was a small-sample mirage; standalone |t|<1,
  orthogonalized marginal |t|≤1.45 (<2.0), cost gate fails ~8×. Honest null.
- **H1 (vwap_dev cost-conditioning)** — killed 2026-06-15, CONFIRMED at depth 2026-06-16: the reversion
  carrier is overwhelmingly an ILLIQUID-name phenomenon (illiq/liq |IC| 6–10× at 126 days) and the liquid
  tier (the only tradeable one) fails cost net. Cost-conditioning is structurally doomed. Definitively dead.

## Standing position after H1+H2+H3 kills (2026-06-16)
- The ONLY canary-clearing signal is `vwap_dev` reversion (t −2.76), uneconomic net-of-cost; no
  microstructure conditioner rescues it (H1 liquidity, H2 flow, H3 depth/spread all killed). The binding wall
  = gross-signal-per-trade vs a fixed ~6–11 bps RT cost; conditioning attacks the wrong side.
- **Unattacked lever = HORIZON → H9** (longer-horizon H60/H120 vwap_dev reversion: amortize the fixed cost
  over a larger move + cut turnover; `2026-06-16-h9-longhorizon-reversion/`, RUNNING). If H9 also kills,
  vwap_dev is dead at all tradeable horizons and the hunt pivots to LOW-TURNOVER NON-price families
  (events H4/H5; fundamentals), NOT another price-reversion variant.
