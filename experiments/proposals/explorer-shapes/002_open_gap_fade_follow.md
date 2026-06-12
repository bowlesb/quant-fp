# Proposal 002 — Open-gap fade/follow, conditional (SHAPE 1)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)
**Cost-structure rank: #2.** LOW-TURNOVER (one decision/name/day, held to close). Needs proposal 000 + one new label.

## Hypothesis (mechanism story)
The overnight gap (today's 09:30 open vs prior 16:00 close) does not resolve neutrally. Two competing
mechanisms: **fade** (gaps over-extend on overnight retail/illiquidity and revert toward prior close
intraday) vs **follow** (gaps carry genuine information and continue). Which one dominates is
**conditional** on gap size and overnight volume: small gaps on light volume tend to fade (noise);
large gaps on heavy volume tend to follow (information). We have `gap_from_open` as a FEATURE but have
NEVER used it as the strategy AXIS with a gap-anchored label.

## Why it is cost-advantaged
ONE decision per name per day, taken at the open and held to the close → far lower turnover than 30m
rebalancing (the killer). Restrict to the liquid head so the single round-trip is in the cheap tier.
Can run long-biased (buy the names predicted to follow up / fade up) to dodge short-underfill.

## Label (NEW — coordinate computation via the Lead)
`open_to_close`: simple return from the 09:30 RTH open bar to the 15:59 close bar, then
cross-sectionally demeaned via `cross_sectional_excess` (reuse `quantlib/labels.py` machinery; same
breadth floor MIN_CROSS_SECTION=20). Source prices from the **daily_session_price helper (proposal
000)** — open_0930 and close_1600 columns — so NO per-experiment bar re-scan. One label row per
(symbol, trade_date).

## Features / conditioning axes (all from the helper + existing panel)
- `gap` = open_0930 / prior_close_1600 - 1  (prior-day close from the helper, shifted one trading day).
- `overnight_vol_proxy` = first-30-min volume vol_0930_1000 / trailing ADV (gap "conviction").
- prior-day range (from helper high/low or existing range feature).
Model the open_to_close label on {gap, gap×vol, prior-range} — does the SIGN of the gap-effect flip
with the conditioning (fade in one regime, follow in the other)?

## Pre-registered result that would FALSIFY
If the gap term carries NO within-timestamp rank-IC on the open_to_close label (|IC| < ~0.01,
Newey-West t < 2) in ANY conditioning regime, AND the net-of-cost L (or L/S) book is ≤ 0 — the gap
axis is dead and we drop it. Pre-registered prior: ~35% the conditional (gap×vol) version shows a
sign-coherent, t>2 effect; gap effects are well-documented but often arbitraged out of liquid names.

## Gates (all present)
- **Shuffle canary** on the open_to_close label.
- **Survivorship neutralization** (per-symbol demean).
- **Net-of-cost** with per-name half-spread + fill-asymmetry haircut; report ONE round-trip/day cost.
- **Turnover honesty:** realized turnover here is structurally low — REPORT it (this is the selling
  point) but verify it empirically, don't assume.
- **PIT:** prior_close must be the PRIOR trading day's close (no same-day leakage); gap uses only
  pre-open information.

## Cheapness
★ once proposal 000 lands (then it's a join + one new label compute, minutes). BLOCKED on 000.

## Lead disposition
<!-- Lead fills -->

## LEAD DISPOSITION — APPROVED, sequenced AFTER helper 000, 2026-06-12
Validated: gates present; conditional gap×volume mechanism is a genuinely different (open-anchored,
one-decision/day, low-turnover) shape; needs ONE new label (open_to_close) + the helper. BLOCKED only on
common_daily_session_price (000). The low-turnover/one-round-trip-per-day structure is exactly the cost-
advantaged profile we want — REPORT realized turnover empirically (don't assume). PIT: prior_close must be
the PRIOR trading day (you flagged it). Build family/shape script once 000 lands; deliver the open_to_close
label compute via quantlib.labels machinery (cross_sectional_excess, MIN_CROSS_SECTION=20). ENQUEUE on
delivery. This is part of the shapes lens's path to >=3 completed runs — sequence it right behind 001/003.
