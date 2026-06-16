# Leads — Modelling Agent (MA), append-only, single-writer

Ranked promising leads with evidence + proposed next action. A lead enters here only with REAL numbers
from a pre-registered test. Empty leads / hunches stay in `BACKLOG.md`, not here.

_(Standing position from `docs/EXPERIMENTS.md`: NO price-only edge has cleared the 4-gate battery; the
strongest historical candidate, the conditional gap-fade, was KILLED by the tradeable-entry trap. **UPDATE
2026-06-16: H2 OFI — the only NON-price lead to clear a 3-day canary — was KILLED on a powered 250-name ×
20-day panel (did not replicate; standalone |t|<1, orthogonalized marginal |t|≤1.45<2.0, cost gate fails
~8×). H3 (depth/spread conditioner) ALSO killed. NO price-only OR order-flow cross-sectional signal clears
the cost gate, and NO microstructure conditioner rescues vwap_dev. H9 (longer-horizon H60/H120) ALSO KILLED —
vwap_dev REVERSES into momentum past 30 min (gross −13 to −34 bps) and turnover stays ~0.90, so horizon
doesn't help either. **vwap_dev reversion is now DEAD at ALL tradeable horizons (15–120 min) under ALL
conditioners. No price/order-flow cross-sectional signal survives. PIVOT to LOW-TURNOVER NON-PRICE event
families (H10 EDGAR drift / H5 dividends / H4 splits).)**_

| date | lead | evidence (real) | net-of-cost | next action |
|---|---|---|---|---|
| 2026-06-16 | ~~**H2 OFI / signed-flow**~~ **KILLED at depth** (the 3-day signal did NOT replicate on a powered panel) | RETEST on the COMPLETE `/store/raw`: **250 liquid names (incl megacaps) × 20 days, 1.57M sym-min**, true CKS OFI from quotes (`2026-06-16-h2-retest-ofi-orthogonal/`). Standalone `ofi_15` rank-IC **−0.0017 (t −0.38)** at H15 — INSIDE the canary band; EVERY OFI/sv signal |t| < 1. Orthogonalized over vwap_dev: best marginal = `sv_15_norm` **t +1.45** (< the 2.0 bar); OFI marginal |t| ≤ 0.89. Only `vwap_dev_15` clears canary (IC −0.0233, t −2.76). | **FAILS**: decile L/S gross **0.35–0.86 bps « 6.41 bps** round-trip spread (~8× short). | KILLED, no re-open in this regime. `order_flow_imbalance` spec SHELVED. The clean `signed_trade_ratio` primitive still SHIPS (PR #33, parity+correctness, an input not a signal). |
| 2026-06-16 | ~~**H3 book depth/spread as a vwap_dev CONDITIONER**~~ **KILLED** | reuse the H2 panel (250×20). Flat vwap_dev H15 net **−10.45 bps** (gross 0.79, spread cost 11.24). Best cell = tight-spread tercile: SLASHES cost (2.70 vs 11.24) but gross there only **0.38 bps** (net −2.32), within **0.04 bps** of the canary max (−2.36) = noise. Depth + size-imbalance terciles add nothing. (`2026-06-16-h3-depth-conditioner/`). | FAILS — net negative beyond the canary in every cell. | KILLED. The microstructure-CONDITIONING branch (H1–H3) is CLOSED — conditioning lowers cost but can't manufacture signal. Next lever = HORIZON (H9). |
| 2026-06-16 | ~~**H9 longer-horizon (H60/H120) vwap_dev reversion**~~ **KILLED** | 300 liquid names × 50 days, 4.86M RTH bars (`2026-06-16-h9-longhorizon-reversion/`). vwap_dev REVERSES into momentum past 30 min: every (W,H) cell gross **−12.7 to −33.7 bps** (worsens with horizon), day-clustered t −1.75 to −2.58. Turnover stays ~**0.90** at H60 AND H120 — the cost-amortization mechanism never engages. | **FAILS** at every cost level — best cell net@6bps **−18.05 bps**; even @4bps every cell −16 to −37 bps. | KILLED. vwap_dev reversion is DEAD at ALL tradeable horizons (15–120 min). H6 (vol conditioner) DE-PRIORITIZED. **PIVOT to event families (H10/H5/H4).** |
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

## Standing position after H1+H2+H3+H9 kills — the PRICE branch is CLOSED (2026-06-16)
- `vwap_dev` reversion was the ONLY signal to clear a shuffle canary (t −2.76 at H15). It is now DEAD:
  uneconomic net-of-cost at 15–30 min (H1), un-rescuable by any microstructure conditioner (H1 liquidity,
  H2 flow, H3 depth/spread), and it REVERSES INTO MOMENTUM at 60–120 min (H9: gross −13 to −34 bps, turnover
  unchanged ~0.90). No price-only OR order-flow cross-sectional signal survives the cost gate at ANY horizon.
- **Durable meta-lessons:** (1) conditioning lowers cost but can't manufacture signal — it attacks the wrong
  side of the constraint; (2) the binding wall is GROSS-SIGNAL-vs-FIXED-COST, and minute/hour price signals
  on liquid names are too weak to clear ~6 bps round-trip; (3) reversion decays inside 30 min and flips to
  momentum after — so there is no "hold longer" rescue.
- **PIVOT (pre-registered `2026-06-16-event-families-pivot/`, data ask routed to the Lead): LOW-TURNOVER,
  NON-PRICE, EVENT-driven signals** — re-priced over DAYS so the fixed cost is a tiny fraction of the move.
  Ranked **H10 (EDGAR 8-K/Form-4 multi-day drift — collector LIVE) → H5 (dividend timing, prior survivor) →
  H4 (splits, likely underpowered)**. This is a genuinely different signal class, orthogonal to the entire
  dead price/microstructure branch. NO further price-reversion variant is worth running.
