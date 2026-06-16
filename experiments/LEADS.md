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
families (H10 EDGAR drift / H5 dividends / H4 splits).** → **★ FIRST CANDIDATE LEAD (2026-06-16): H10 8-K
EVENT DRIFT** — 8-K cohorts drift +2.95/+5.69/+5.53% (per-symbol-DEMEANED, day-clustered t 1.97/3.05/2.96) at
1/3/5d, clear canary, cost wall non-binding (multi-day horizon). NOT yet a confirmed lead — first KEEP must
survive a walk-forward OOS + the PEAD/survivorship scrutiny (H10b, RUNNING). The first non-price signal to
even reach the escalation gate.)_

| date | lead | evidence (real) | net-of-cost | next action |
|---|---|---|---|---|
| 2026-06-16 | **★ H10 8-K EVENT DRIFT — FIRST KEEP (in-sample); ESCALATING to OOS** | `filings` 8-K cohorts vs same-date controls, D+1 entry (look-ahead-safe `available_at`), 126 days, ~11.5–11.9k obs/120+ dates (`2026-06-16-h10-edgar-event-drift/`). Per-symbol-DEMEANED drift **+2.95% (t 1.97) 1d / +5.69% (t 3.05) 3d / +5.53% (t 2.96) 5d**, clears 10-seed canary at all three. **10d KILLED** (demean collapse = trending-stock artifact). **Form-4 KILLED** (negative raw alpha fully evaporates under demean = style bias, no buy/sell direction). | **Cost non-binding** — ~6 bps round-trip vs +295–569 bps alpha (the multi-day-horizon thesis: this is the FIRST place cost isn't the wall). | **DO NOT trust the magnitude yet — first KEEP, in-sample, one up-market regime, survivorship-imperfect, pooled (PEAD-suspect).** ESCALATING (H10b RUNNING): walk-forward OOS (demeaned t≥2 OOS required), earnings(2.02)-vs-non-earnings split (is it just PEAD?), liquid-tertile survivorship stress, D+1-OPEN tradeable entry. If it survives → parity-safe 8-K event-flag feature proposal to the Lead. |
| 2026-06-16 | ~~**H2 OFI / signed-flow**~~ **KILLED at depth** (the 3-day signal did NOT replicate on a powered panel) | RETEST on the COMPLETE `/store/raw`: **250 liquid names (incl megacaps) × 20 days, 1.57M sym-min**, true CKS OFI from quotes (`2026-06-16-h2-retest-ofi-orthogonal/`). Standalone `ofi_15` rank-IC **−0.0017 (t −0.38)** at H15 — INSIDE the canary band; EVERY OFI/sv signal |t| < 1. Orthogonalized over vwap_dev: best marginal = `sv_15_norm` **t +1.45** (< the 2.0 bar); OFI marginal |t| ≤ 0.89. Only `vwap_dev_15` clears canary (IC −0.0233, t −2.76). | **FAILS**: decile L/S gross **0.35–0.86 bps « 6.41 bps** round-trip spread (~8× short). | KILLED, no re-open in this regime. `order_flow_imbalance` spec SHELVED. The clean `signed_trade_ratio` primitive still SHIPS (PR #33, parity+correctness, an input not a signal). |
| 2026-06-16 | ~~**H3 book depth/spread as a vwap_dev CONDITIONER**~~ **KILLED** | reuse the H2 panel (250×20). Flat vwap_dev H15 net **−10.45 bps** (gross 0.79, spread cost 11.24). Best cell = tight-spread tercile: SLASHES cost (2.70 vs 11.24) but gross there only **0.38 bps** (net −2.32), within **0.04 bps** of the canary max (−2.36) = noise. Depth + size-imbalance terciles add nothing. (`2026-06-16-h3-depth-conditioner/`). | FAILS — net negative beyond the canary in every cell. | KILLED. The microstructure-CONDITIONING branch (H1–H3) is CLOSED — conditioning lowers cost but can't manufacture signal. Next lever = HORIZON (H9). |
| 2026-06-16 | ~~**H11 longer-horizon intraday MOMENTUM** (the sign-flip H9 revealed)~~ **AMBIGUOUS → effectively KILL at pre-registered scope** | 300 liquid × 49 days, timezone-CORRECTED (`2026-06-16-h11-longhorizon-momentum/` v2). Momentum L/S (long top-vwap_dev, short bottom, H60/H120) clears canary in W30 cells, SURVIVES per-symbol-demean (not survivorship), structurally CLEAN of the open print (vwap_dev null at 09:30 by W-bar lookback — gate a legit no-op, verified). BUT corrected gross only **+3 to +16 bps** (v1 +12 to +34 was the off-by-240 timezone-bug inflation, 2–3×); best demeaned cell W30/H120 **net@6 +9.10 bps but t=1.51 < 2**. | W30 clears canary but t<2; W60 FAILS canary. Marginal, NOT a KEEP. | Full-session momentum misses the KEEP bar. ONE anomaly worth a CLEAN test: mid-session (10:00–15:30 ET) W60/H120 → t=3.27 / net@6 +20 bps, but POST-HOC (2–3 slots), high overfit risk → **H12: pre-register mid-session-only with a HOLD-OUT / longer window before believing it.** |
| 2026-06-16 | ~~**H9 longer-horizon (H60/H120) vwap_dev reversion**~~ **KILLED** | 300 liquid × 50 days (`2026-06-16-h9-longhorizon-reversion/`). vwap_dev reverses into momentum past 30 min; reversion L/S net-negative at every cost level. **NOTE: H9's gross magnitudes (−12.7 to −33.7 bps) used the SAME off-by-240 timezone bug H11 later found — inflated 2–3×; CORRECTED reversion gross ≈ −3 to −16 bps.** KILL conclusion still holds (corrected reversion still net-negative after ~6 bps cost) but this row's magnitudes are overstated. | **FAILS** at every cost level (corrected: net-negative). | KILLED. vwap_dev reversion dead at all tradeable horizons. The MOMENTUM direction (H11) is the only thing that even clears canary — and only marginally (W30, t<2). |
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
