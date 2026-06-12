# Proposal 004 — Opening-range breakout, liquid-head only (SHAPE 2)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)
**Cost-structure rank: #4.** SPARSE (fires only on names that break). Single-name time-series shape (different axis). Needs proposal 000 + one new label.

## Hypothesis (mechanism story)
A name that breaks above its 09:30-10:00 HIGH (or below its LOW) tends to continue in the breakout
direction for the rest of the session — the classic opening-range breakout. Mechanism: the first 30
minutes establishes a consensus range; a break signals an order-flow imbalance strong enough to clear
the range, which persists as the imbalance works through the day. Conditioning: break on HIGH
first-30-min volume (vs ADV) is more informative than a low-volume drift-out.

## Why it is a genuinely different shape
This is a SINGLE-NAME TIME-SERIES signal (each name vs its OWN opening range), not a cross-sectional
rank. Every shape tested here so far is cross-sectional — this is a different axis entirely.

## Why it is cost-advantaged
SPARSE: it only fires on names that actually break the range on a given day (a minority). One
decision/name/day, held to close → low turnover. Restrict to the liquid head → cheap-tier round-trip.
Long-only variant (trade only UP-breaks) dodges short-underfill.

## Label (NEW — coordinate via the Lead)
`ten_to_close`: simple return from the 10:00 price to the 15:59 close (the post-opening-range window),
from the **daily_session_price helper (proposal 000)** — px_1000 and close_1600. Demeaning optional
(this is a time-series shape; the raw direction is the signal). One row per (symbol, trade_date).

## Features (all from the helper)
- `break_up` = px_1000 > high_0930_1000 (did it close the first-30-min above the range high?); likewise
  `break_down`. (Refinement: use an intra-window break flag if cheap, else the 10:00 vs range proxy.)
- `position_in_range` = (px_1000 - low_0930_1000) / (high_0930_1000 - low_0930_1000).
- `or_volume_z` = vol_0930_1000 / trailing ADV (breakout conviction).

## Pre-registered result that would FALSIFY
If `ten_to_close` return is independent of the break direction/position-in-range (no monotonic
relationship, breakout-cohort mean return indistinguishable from non-breakout, |t| < 2) AND the
net-of-cost long-only book is ≤ 0 — ORB is dead in this universe. Pre-registered prior: ~30%; ORB is
folklore-popular but well-arbitraged in liquid names and notoriously cost-sensitive.

## Gates (all present)
- **Shuffle canary** on ten_to_close.
- **Survivorship neutralization** (per-symbol demean of the breakout-cohort returns).
- **Net-of-cost:** per-name half-spread + fill-asymmetry; one round-trip/day; report on the LIQUID
  subset (ORB on illiquid names is a cost trap).
- **Turnover honesty:** report realized participation (fraction of name-days that fire) and turnover.
- **PIT:** the break decision uses ONLY 09:30-10:00 data; the trade is placed at/after 10:00 — no
  same-window look-ahead.

## Cheapness
★ once proposal 000 lands. BLOCKED on 000.

## Lead disposition
<!-- Lead fills -->

## LEAD DISPOSITION — APPROVED, sequenced AFTER helper 000 (lower priority), 2026-06-12
Validated: gates present; genuinely different AXIS (single-name time-series vs cross-sectional rank) —
valuable for diversity even at a low prior (~30%, ORB is folklore-popular and well-arbitraged in liquid
names, cost-sensitive). Needs helper 000 + the ten_to_close label. Build after 002 (both share the helper
+ open-anchored label machinery, so do them together once 000 lands). Long-only up-break variant to dodge
short-underfill is the right call. REPORT realized turnover + the break-cohort size honestly. ENQUEUE on
delivery. Contributes shape-diversity to the Monday deliverable.
