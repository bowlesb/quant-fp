# Edge Meta-Synthesis & Next-Hypothesis Pre-Registration — 2026-06-25

**Author:** Modeller (background research cycle)
**Code SHA:** `1d46b52` (origin/main HEAD: `1d46b52 fix(dashboard): restore boot — dep-closure guard #462`)
**Data state:** order-flow trade tape ~7,600 syms + NBBO quotes ~4,031-sym breadth since **2026-03-31**
(531-sym head-only set reaches back to 2024-12); settled raw bars 2018-2025 deep; the trusted feature store
(dense ≥500-sym coverage on ~46 dates 2026-04-15..06-18). No new data landed this cycle.
**Compute:** light cycle (reading + ledger/experiment synthesis only; no panel build, no GBM run — live
capture untouched).
**Purpose:** an honest, auditable map of every edge lane tried → verdict → the DECISIVE reason it died, then
the single highest-EV UNTESTED direction, pre-registered so the next cycle just RUNS it.

---

## TL;DR

1. **The alpha search is picked-over.** Every direction-of-price and microstructure-alpha lane is a settled
   NO-GO, now across THREE order-flow substrates (bar-agg, quote-dynamics, per-tick) and multiple framings.
   The recurring result is a law, not a coincidence: **magnitude/intensity is predictable, direction is not,
   and the magnitude that is predictable is efficiently priced** (into spreads, into option premia).
2. **The prior #1 strategic move — combine the real-but-weak signals into a portfolio — was RUN and came
   back NULL** (the streams are +0.77 correlated; there is no free diversification because the baseline
   already prices the shared structure). So "the edge is the portfolio, not any one signal" is now a CLOSED
   finding, not an open hope.
3. **The honest state: the highest-EV remaining work is NOT a new alpha hunt.** It is (i) a FREE cost-model
   refinement explicitly left on the table by the last two post-mortems, and (ii) deployment-hardening the
   net-positive baseline we already have. The strongest genuinely-untested ALPHA idea — regime-conditional
   feature interactions — is **marginal** (direction is a 10×-settled null; conditioning is the last place an
   unconditional screen could have hidden an effect, but the prior is low). I pre-register it anyway because
   it is cheap, decisive, and the one alpha stone genuinely unturned — but with an explicit "this is the
   marginal best, not a high-conviction bet" label.

---

## 1. THE CLOSED-LANE INVENTORY (every lane → verdict → DECISIVE reason it died)

| Lane | Verdict | Decisive reason it died | Ref |
|---|---|---|---|
| Price-DIRECTION (intraday/overnight, ~5 framings) | NO-GO | No signal — direction unpredictable cross-sectionally at our scale (NW\|t\|<1.2, IC < shuffle) | pre-session |
| Per-minute LOOK-AHEAD labels (triple-barrier, fwd-runup) | NO-GO | up_move_start 0/32; the "passes" were fwd-runup = vol circularity (magnitude not direction), 10th repro | #326 |
| Order-flow CROSS-SECTIONAL (bar-agg OFI, 0/4) | KILL | No signal — trade_freq/book_depth dominate gain = intensity, not alpha | #orderflow |
| Order-flow QUOTE-DYNAMICS (spread vol/imbalance/staleness) | NO-GO | Real gross ranking gain (AUC .529→.536) but DIES net-of-cost at every conservative cut (binding-constraint failure) | #268/#275 |
| Order-flow PER-TICK (Lee-Ready signed-notional/block/persistence) | NO-GO | Same 0/4 null on the FINEST substrate; +$ was lone-outlier-tightest-cut (per-day t<2 at the $-driving cut), AUC/rankIC DOWN | this session (06-25) |
| HF03 spread-capture | KILL | (microstructure liquidity-provision framing) net-negative | #hf03 |
| Price-GEOMETRY (swing_dc as $) | NO-GO | Redundant — baseline already prices the path/vol structure (AUC .535→.533) | #259 |
| Price-GEOMETRY (path-geometry G0) | NO-GO | Redundant — ~91% of tail edge already in baseline shape+vol groups | #263 |
| Vol/MAGNITUDE forecast edge (proxy straddle) | SHELVED | Signal REAL & incremental but no forecast-$ net-of-cost — vol is efficiently priced INTO the premium | #331 |
| Vol/MAGNITUDE — real ATM-IV cross-sectional screen | SHELVED | IV efficiently prices forecastable vol (the proxy was adequate; H0 held) | #vol-implied |
| Lane D EDGAR / sector / news | NO-GO (for return) | An information-arrival→PARTICIPATION (volume) effect, not directional alpha | #187/#197 |
| Market-REGIME scalar screen | NO-GO (for return) | Survives only as `mkt_absret` turbulence → fwd VOLUME (gather group, not alpha); direction = floor | 06-20 |
| Weekly-REVERSAL (multi-day) | NO-GO | Real +0.025 IC, 11σ vs shuffle, own-vol-independent — but per-week $ basket NW-t<2 AND −13bps survivorship haircut flips it negative | #287 |
| Multi-signal PORTFOLIO combination | NULL | Streams are +0.771 correlated → no diversification to harvest; combined Sharpe LOWER than the better single stream | #288 (06-20) |

**The map reduces to one sentence:** there is no un-priced direction signal at our scale, the predictable
magnitude is efficiently priced, the real-but-weak signals are mutually correlated (no portfolio lift), and
the one clean signal (weekly-reversal) is blocked by survivorship — a DATA problem, not a model problem.

---

## 2. WHAT IS GENUINELY UNTESTED (and is it worth testing?)

Three candidates clear "not already done." I rank by EV and apply the sanity bar honestly.

### Rank 1 (highest EV, NOT alpha) — FREE effective-vs-QUOTED cost G2-incremental
The last two post-mortems (#268/#275 quote-cost; 06-25 tick-cost) BOTH explicitly flag the same unturned
stone: the effective-cost model never emitted the **quoted-spread label column**, so the decisive
"effective beats quoted" comparison — the only thing that justifies re-wiring `_attach_realized_half_spread`
— is **unmeasured**. It is FREE on the current window (no backfill: both labels derive from the
`realized_half_spread_bps_multi` already in the panel). EV is real but bounded: it is a COST refinement, not
alpha; it can only LOOSEN or TIGHTEN existing net-of-cost verdicts on the liquid tier, not open a new lane.
**This is the cheapest decisive open item, but it is infrastructure, not edge.** Recommend it as a quick
parallel task, not the headline.

### Rank 2 (the deployment asset, NOT a hunt) — harden the net-positive baseline toward deployment
The trusted-baseline model is net-positive under REALIZED (Stage-1 measured) cost: +$123,579 headline-10%,
Sharpe 18.9 (#271). The 06-20 meta-synthesis already named this move (c) as #2-do-in-parallel, and the
portfolio NULL (#288) re-routed everything here. This is the highest-EV move on the board, but it is
hardening/validation (regime splits, capacity/turnover under realized cost, the Stage-2 cost-timing gate),
NOT a Modeller alpha experiment. **Owner = Lead/deployment track, not this hunt.**

### Rank 3 (the marginal-best ALPHA, genuinely untested) — REGIME-CONDITIONAL feature interactions
Every alpha screen we ran was **UNCONDITIONAL**: one cross-sectional IC over the whole panel. An effect that
exists only in a vol/liquidity regime (e.g. order-flow imbalance predicts direction ONLY in high-vol, low-
liquidity names where price impact is real, and is washed out by the calm-regime majority) would be
INVISIBLE to every screen on the inventory. The 06-20 regime screen conditioned the *market-aggregate
forward* on a *market scalar* (found turbulence→volume) — it did NOT test **cross-sectional feature ×
regime interactions on a per-name forward return**. That specific cell is unturned.

**Honest sanity-bar verdict: MARGINAL.** Direction is a 10×-settled null and conditioning is the textbook
last refuge of a p-hacker (every added regime split is a fresh fishing degree of freedom). The prior is LOW.
But it clears "genuinely untested," it is CHEAP (existing store + existing features, no new data), and it is
decisive (a disciplined conditional screen with locked regimes either finds a robust interaction or closes
the last alpha door). It is the strongest remaining alpha idea precisely BECAUSE it is the only place an
unconditional screen structurally could not look. I pre-register it (below) with maximal anti-fishing
discipline — locked regimes, locked features, FDR across the full interaction grid, per-day-t + shuffle +
lone-outlier gates — so the fishing surface is closed before any number is seen.

### Rejected (already-tested or implausible)
- **Liquid tick-TIER microstructure** (a narrower tradeable universe than the full-universe order-flow
  screen): TEMPTING but the 06-25 tick G0 already ran on the **top-200-liquid/day** panel — that IS the
  liquid tick-rich tier. The per-tick Lee-Ready signal was NO-GO precisely THERE. Narrowing further only
  shrinks N and worsens power; it is not a genuinely different test. REJECTED as effectively-done.
- **A fresh single-signal invention batch** (batches 1–4 are exhausted): each new feature A/B's against a
  baseline that already prices it (#255); the marginal-over-shipped discipline has driven these to
  diminishing returns. REJECTED as a worked vein.

---

## 3. THE PICK

**Pre-register Rank 3 (regime-conditional feature interactions) as the next-cycle experiment — labeled
explicitly as the MARGINAL-BEST remaining alpha, not a high-conviction bet.** It is the single genuinely-
untested alpha direction grounded entirely in data we already have, it is cheap and decisive, and a
disciplined NO-GO closes the last alpha door cleanly (high informational value even if — likely — null).

In parallel (NOT Modeller-owned, flagged to Lead): run the FREE effective-vs-quoted cost G2-incremental
(Rank 1) and continue baseline deployment-hardening (Rank 2).

**If the regime-conditional screen also nulls, the honest conclusion is that the cross-sectional alpha space
is exhausted on the data we have, and ALL future Modeller EV is in (a) the delisting-inclusive universe
acquisition that would make weekly-reversal's clean +0.025 IC tradeable, and (b) deployment-hardening — not
in more hunting.** That is the strategic fork the next cycle should be ready to take.

PRE_REGISTRATION.md (Rank 3) accompanies this file.
