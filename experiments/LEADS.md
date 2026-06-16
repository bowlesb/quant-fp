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
families (H10 EDGAR drift / H5 dividends / H4 splits).** → H10 8-K drift was the FIRST KEEP and the first
signal to reach the escalation gate, **but H10b KILLED it for TRADEABILITY: the OOS effect is REAL (1d t 2.71)
yet lives ENTIRELY in the illiquid bottom 2/3 — the liquid (tradeable) tertile is dead (OOS t ~0.3–0.5). The
H1 illiquid-concentration trap in event clothing; the alpha can't be harvested.** Standing position: every
signal that clears a canary — price reversion (H1), 8-K drift (H10) — is ILLIQUID-CONCENTRATED and dies in
the liquid tier. The open question: does ANY signal class produce a LIQUID-tradeable edge? Next: H5/H4
(independent event families) under the same liquid-tradeability gate.)_

| date | lead | evidence (real) | net-of-cost | next action |
|---|---|---|---|---|
| 2026-06-16 | ~~**H5 dividend POST-EX drift**~~ **KILLED — NO signal anywhere** (a harder null than H10) | `corporate_actions_pit`, 6,042 ex-div events, D+1-open entry, liquid-tertile-PRIMARY + walk-forward OOS (`2026-06-16-h5-dividend-postex-drift/`). **Liquid-tertile OOS demeaned best \|t\|=0.67** (1d +0.51 / 3d −0.67 / 5d −0.38 / 10d +0.31) — nowhere near t≥2. UNLIKE H10 this is NOT an illiquid mirage: the FULL universe is ALSO dead (OOS t 0.02–0.86). The explorer self-killed a tempting high-yield/10d OOS t=3.08 (zero IS support, below canary, post-hoc, N=173 — textbook false discovery, per the hold-out rule). | dead at every horizon, every tier, IS and OOS — no cost question to ask. | KILLED. Post-ex dividend drift does not exist in this universe/window. The documented Elton-Gruber effect is arbitraged away in listed large/mid caps. |
| 2026-06-16 | ~~**H10 8-K event drift** (first KEEP in-sample)~~ **KILLED FOR TRADEABILITY at escalation** — the H1 illiquid trap in event clothing | In-sample (`2026-06-16-h10-edgar-event-drift/`): 8-K cohorts drift +2.95/+5.69/+5.53% demeaned (t 1.97/3.05/2.96) 1/3/5d, canary-clear. **H10b escalation (`2026-06-16-h10b-8k-drift-escalation/`) walk-forward OOS HELD** (1d OOS t 2.71, 3d 2.56 — the effect is REAL, not in-sample overfit) **— but the LIQUID-TERTILE is DEAD: OOS demeaned t 0.54 / 0.31 / −0.02.** The entire alpha lives in the illiquid bottom 2/3 (stale-price diffusion in thin names). Earnings-split inconclusive (7% item-code sample) but earnings-8K OOS t≈0 → NOT PEAD. | **UNHARVESTABLE**: illiquid-tertile round-trip cost 30–200 bps/side ≫ the alpha; liquid (tradeable) tertile has NO signal. Same wall as H1 (vwap_dev was illiquid-concentrated too). | **DO NOT promote to a feature** — the alpha cannot be harvested in tradeable names. A real signal but not a real EDGE. Possible reframe H10c (liquid-only 8-K conditioned on filing-type / volume-shock) but LOW prior (20%) given the liquid extinction. Higher-value next: H5/H4 (INDEPENDENT event families) — does ANY event family survive the liquid-tradeability gate? |
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

## Standing position after the EVENT-FAMILY cycle — 0/3 tradeable, two failure modes (2026-06-16)
- **Scoreboard: 0/3 tradeable edges.** H1 (vwap_dev reversion) and H10 (8-K drift) = ILLIQUID MIRAGE (real
  full-universe signal, OOS-replicating, but DEAD in the liquid tradeable tertile — alpha lives in stale-price
  illiquid names). H5 (dividend post-ex drift) = NO SIGNAL ANYWHERE (dead even in the full illiquid universe).
- **The sharpened question (two live tests):** (a) does ANY signal class live in LIQUID tradeable names? —
  H4 (split drift) RUNNING, but expected weak (244 events, distress-illiquid by nature). (b) are the illiquid
  mirages (H1/H10) actually HARVESTABLE at Ben's ~$5–10K/position scale where cost is ~3–7 bps not 30–200? —
  **H13 small-capital re-cost RUNNING with a hard CAPACITY-CEILING gate.** H13 is the higher-EV of the two:
  the illiquid signals are REAL (they cleared OOS), and the only thing that killed them was an institutional
  cost model that may not apply to a $100K book. If H13 shows the illiquid H10/H1 alpha nets positive at
  $5–10K/name with capacity ≥ ~$100K → the FIRST harvestable edge (small-capital illiquid). If it dies even
  at small size → the illiquid signals are dead for Ben too and we commit to the (so-far-empty) liquid hunt.
- **Strategic fork (Ben's call, surfaced via the Lead):** LIQUID-SCALABLE (no edge found yet) vs
  ILLIQUID-SMALL-CAPITAL (H13 decides if it's real at his scale). The hunt's next direction depends on H13 +
  H4.

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
