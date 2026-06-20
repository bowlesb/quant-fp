# 8-K EVENT-STUDY — VERDICT

**Date:** 2026-06-19 · **Pre-reg:** `prereg.md` (written before any outcome) · **Panel:** 3,769 8-K events,
1,764 symbols, 2018→2025 (3,349 off-hours / 420 RTH), liquidity-gated (≥250 RTH bars/day), entry strictly
after `available_at + 5min`, DST-correct ET anchoring. Stats: `screen.py` → `screen_results.csv`.

## TL;DR — H1 confirms (intensity), H2 is REAL-but-NOT-tradeable, H3 is the 5th direction-null. No edge.

1. **H1 — VOLUME/participation surge at the 8-K instant: CONFIRMED, but it's intensity, not alpha.** The
   event window has 1.4–1.5× the name's own-baseline volume (median log-ratio +0.31→+0.42 across 5–60m),
   shuffle-z 19–28, OOS-consistent, survives BY-FDR. BUT it **collapses under the own-vol control**
   (collapse 0.02–0.08) — most of it is "this is a volatile/active name," same as #187's EDGAR→volume.
   This VALIDATES the event framing + is the strategy-battery's single-name faithfulness anchor; it is NOT
   a tradeable directional/vol edge.

2. **H2 — abnormal MOVE-MAGNITUDE: REAL but NOT tradeable net-of-cost (the honest likely outcome).**
   - The gross magnitude measures (`range_abn`, `rv_abn`) have huge z (14–30) but **collapse under the
     own-vol control** (collapse 0.01–0.11) — same vol-persistence story as the volume.
   - The directional move `absret_abn` does NOT collapse (collapse 0.68–0.93, z 2.9–3.9) — the event
     genuinely moves the price more than baseline, net of ambient vol. **This was the prize candidate.**
     BUT two decisive checks kill the tradeable claim:
     - **Net-of-cost is tail-driven, not a real edge.** At 30m the abnormal move yields a positive MEAN net
       (+61 bps @5bps) but the **MEDIAN net is +9.7 → −0.3 bps at 10bps**, win-rate ~50%. The positive mean
       is entirely a fat right tail (rare big movers); the median event does NOT beat a straddle-cost proxy
       + round-trip. A vol/straddle bet around the 8-K is **not net-positive on the typical event.**
     - **The effect is regime-split and direction-INCONSISTENT.** Off-hours `absret_abn` is POSITIVE
       (median log-ratio +0.21, z+4.5) but the clean intraday **RTH subset is NEGATIVE** (median −0.10→−0.19,
       z−4 to −4.7; RTH net-of-cost median **−13→−23 bps**, win 26–34%). The off-hours "edge" is the
       next-session-open residual after an after-hours information move (raw median 104 bps vs RTH 34 bps) —
       the big move already happened in the un-tradeable overnight gap. The clean continuous-bar case (RTH)
       LOSES. A signal that is +offhours / −RTH and median-negative net-of-cost is **not a tradeable vol
       edge.**

3. **H3 — DIRECTION drift post-8K: CLEAN NULL.** Mean signed forward return indistinguishable from zero at
   every window (|t| < 1.3), no FDR survival. **The 5th settled direction-null** (price ×2, order-flow,
   EDGAR+sector #187, now 8-K event-drift).

## Is it tradeable? — NO (honest, no escalation)

Nothing here is a tradeable edge. The volume/participation surge is robust but is the same intensity signal
#187 already found (collapses under own-vol). The abnormal move is real but **median-negative net-of-cost**
and **direction-inconsistent across regimes** — the positive off-hours mean is a fat-tail + un-tradeable
overnight-gap artifact, and the clean RTH case is negative. **No confirmatory replication is flagged** —
the cost + median + regime-split checks already settle it; there is no median-positive tradeable claim to
replicate.

## What it DOES establish (the deliverable)

- A **5th direction-null** — post-8K drift joins the settled set; stop hunting cross-sectional/event
  DIRECTION.
- The **8-K is an INTENSITY event** (volume + gross range/rv up), but the intensity is own-vol-explained and
  the residual move is not cost-positive. This is the same conclusion as #187 (information arrival →
  participation, not alpha), now at the EVENT granularity — a consistent, twice-confirmed picture.
- **Battery faithfulness target** (single-name / Phase-1 archetype): on the 8-K event surface a faithful
  battery archetype must reproduce — volume surge YES, gross-magnitude up but own-vol-explained, abnormal
  move real but median-NEGATIVE net-of-cost, direction NULL, RTH-vs-offhours sign split. A backtest that
  shows a clean tradeable 8-K vol edge is OVERFIT/look-ahead and fails this faithfulness check.

## Method notes / caveats (for the adversarial auditor)

- DST-correct: all RTH bounds + entries anchored in `America/New_York` (an early bug used fixed UTC minutes
  → Int8 overflow + DST drift; fixed, re-validated). Entry = first tradeable bar ≥ target ET minute (sparse
  names enter at the next real bar; >5-min slippage → dropped).
- Own-baseline = the name's own prior ~20 same-time-of-day sessions; the own-normalized ratio IS the
  within-name control (the #187 lesson built into the stat). The own-vol collapse additionally partials out
  the name's baseline rv level.
- $1-floor on entry close; the off-hours/next-session windows carry the overnight-gap caveat (the reason the
  regime split is reported separately — the off-hours number is NOT a clean intraday vol bet).
- Sign-flip permutation (2000 iters) is the event-timestamp shuffle analogue (under no-effect each event's
  log-ratio is sign-symmetric). 60 cells, BY-FDR q=0.10. 3,769 events is a SCREEN; a survivor would warrant
  a deeper disjoint-year replication — nothing survived the cost+median+regime checks that needs it.
