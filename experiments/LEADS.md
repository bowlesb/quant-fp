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
| 2026-06-16 | ~~**H13 small-capital re-cost** of the illiquid H1/H10 signals~~ **KILL — the illiquid edge is unharvestable even at $5K/name** (the decisive result) | Re-scored the illiquid H10 8-K cohort at Ben's scale with a measured/estimated per-name cost + capacity ceiling (`2026-06-16-h13-smallcap-recost/`). **Median illiquid ADV = $35,822 → a $5K order is 14% of daily volume = ~813 bps round-trip (k=10), 440 bps (optimistic k=5)** — 4–8× the ~1.7–3.1% gross OOS alpha. 1%-ADV cap → max **$358/name, CAPACITY CEILING $0**. Does not survive the 2× spread stress. (Cost is an estimate — 32/2,444 illiquid names measured — but a likely UNDER-estimate, so KILL is conservative; the $0 capacity is structural regardless.) | **NET NEGATIVE at every order size + cost assumption.** The small-capital advantage INVERTS: $5K is not small for a $36K/day stock. | KILL. The illiquid signals (H1/H10) are real but unharvestable at ANY capital scale — you ARE the market. The detectability (slow illiquid price discovery) and the untradeability are the SAME property. Commit to liquid hunt / new signal class. |
| 2026-06-16 | **H4 split POST-ex drift** — **UNDERPOWERED / NEEDS H8 backfill** (not a verdict) | `corporate_actions_pit` (`2026-06-16-h4-split-postex-drift/`). REVERSE splits: full-universe drift strongly negative + canary-clear + demean-surviving (alpha −5 to −16%, t −2.4 to −3.6) — REAL, sign-correct (distress continuation). But **only 4 of 312 reverse splits are LIQUID** (1.3% — structural: reverse splits ARE distress events on illiquid names), so the liquid cohort can't be tested (N=4 < pre-committed 20; the 4 are direction-correct + canary-clear but underpowered). FORWARD splits: only 17 total / 9 liquid — untestable, sign-wrong but meaningless at N. | reverse-split drift lives in the illiquid/mid tiers (same as H1/H10); the liquid tier is structurally near-empty. | **NEEDS H8 deep-split backfill** (delisted names + longer history) before any liquid verdict. Reinforces the meta-pattern: event drift is illiquid-concentrated, and for reverse splits it's economic-by-construction. Routed as a data ask. |
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

## ★ CYCLE SYNTHESIS — 0 tradeable edges; BOTH paths closed; the constraint is fully mapped (2026-06-16)
**Scoreboard: 0 tradeable edges from 7 tested hypotheses (H1–H13). Every one pre-registered, cost- and
liquidity-gated. This is a COMPLETE, honest map of where the edge ISN'T — worth more than a fragile lead.**

Three exhaustive findings:
1. **The PRICE / microstructure cross-sectional branch is CLOSED.** vwap_dev reversion (the only
   canary-clearing price signal, t −2.76) is dead at all horizons 15–120 min (H1/H9), un-rescuable by any
   conditioner (liquidity H1, flow H2, depth/spread H3), and flips to momentum at 60–120 min which is itself
   marginal (H11, demeaned t 1.51). OFI/signed-flow killed (H2). No price/order-flow signal survives.
2. **The EVENT / non-price branch is CLOSED for tradeability.** 8-K drift (H10) and reverse-split drift
   (H4) are REAL and OOS-replicating but ILLIQUID-CONCENTRATED — dead in the liquid tier. Dividend drift (H5)
   has no signal anywhere. Form-4 = style bias.
3. **THE DECISIVE H13 RESULT — the illiquid signals are NOT harvestable even at Ben's $5–10K scale.** The
   small-capital reframe (you're a tiny fraction of volume, so cost is low) INVERTS in the illiquid tail
   where the alpha lives: median illiquid ADV is only **$35,822**, so a $5K order is **14% of daily volume =
   ~813 bps round-trip** (8× the alpha). The 1%-ADV capacity cap allows at most **$358/name → capacity
   ceiling $0**. The signal is real; the stocks ARE the market at any tradeable size. (Cost is an estimate —
   only 32/2,444 illiquid names have measured spreads — but it's a likely UNDER-estimate, so the KILL is
   conservative-safe; and the $0 capacity ceiling is structural regardless of spread precision.)

**THE UNIFYING CONSTRAINT (the day's core lesson):** every signal that clears a canary in this universe is
**illiquid-concentrated, and illiquidity is exactly what makes it both detectable (slow price discovery =
the drift) AND untradeable (you move the price you're trying to capture).** The detectability and the
untradeability are the SAME property. Liquid names are efficiently priced (no signal); illiquid names carry
signal you can't extract at ANY capital scale (even $5K is too big). This holds at fund scale AND at $100K.

**WHERE A REAL EDGE COULD STILL LIVE (the honest forward map — for the next cycle / Ben's call):**
- **Higher-frequency execution on a liquid name** (intraday, not the daily cross-section) — where a real-time
  signal + low per-trade cost on a megacap could clear; needs the quote/latency infra (partly built).
- **A signal liquid names DO carry that we haven't tested** — options-implied (vol surface / skew), index
  rebalance / ETF-flow events, cross-asset (rates/FX → equity), genuine fundamentals (not calendar effects).
- **Capacity-aware mid-tier** — the MID liquidity tier (not top, not bottom) sometimes carried borderline
  drift (H4 reverse mid-tier t −2 to −4); a mid-tier signal with a modest capacity ceiling might thread the
  needle for a $100K book, but none cleared cleanly here.
- **Accepting the daily-rebalance illiquid edge is NOT viable** — H13 settles that for this signal class.
- A NEW signal class (events with bigger/faster reactions in LIQUID names; alt-data) is the highest-EV next
  direction. The price + corporate-action-calendar classes are mapped and (mostly) empty.

**Strategic fork resolved by data:** there is no illiquid-small-capital edge in the signals found
(H13 KILL); the liquid-scalable hunt is 0-for-everything so far. → Next cycle pivots to (a) liquid-name
higher-frequency or (b) an untested liquid-carrying signal class (options/flow/fundamentals). Surfaced to
Ben for direction.

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
