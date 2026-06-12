# Data-lens report — Open gap-fade: the panel's strongest signal + its cost blocker (feeds shapes #002)

**Author:** explorer-data | **Date:** 2026-06-12 | **Panel:** v1.1.1 (613 days)
**Status:** ARCHAEOLOGY for explorer-shapes' shape #002 (Lead arbitration: shapes owns the shape, I feed
the data structure). NOT a separate explorer-data backtest. This report is the load-bearing data contribution.

## One-line
At the 9:30 ET open, the overnight gap MEAN-REVERTS hard — the single strongest signal in the panel
(IC −0.072, t −18.5). It trades once/day (lowest turnover). The blocker is the OPENING SPREAD, which the
Lead measured at 6-12bps half-spread — large enough to be the whole question.

## How it was found
Followed the data-integrity thread "is the all-NaN-return 9:30 open cross-section biasing the panel?"
(every intraday-return feature is 100% NaN at the open). The answer inverted the premise: the open isn't
DEGRADED — via gap_from_open (the one feature meaningful at the open) it carries the BEST signal in the panel.
This also resolves the open-cadence modeling question (Lead's ruling): gap_from_open being the open's signal
ARGUES FOR keeping the open cadence in the panel, not excluding it.

## Evidence (within-ts rank-IC of gap_from_open vs forward return)
- **At the open (mod=570): IC −0.0717, t −18.5** over 613 days. At EVERY other cadence: +0.0004 (noise).
  gap_from_open is only meaningful at the open; mid-session it's a stale distance-from-open stat.
- **Liquidity (INVERTED-U — the key tradability nuance):** liq1 −0.063 (t−8.8), **liq2 −0.095 (t−22.6)**,
  **liq3 −0.089 (t−20.2)**, liq4/mega −0.038 (t−7.2). Mega-caps fade LEAST (efficiently priced overnight).
  The signal/cost sweet spot is the MID-liquidity tier (liq2/liq3), NOT the most-liquid decile —
  the opposite of the usual "trade only the most liquid" instinct.
- **Persistence:** −0.060 (t−16.0) at 60m = ~83% retained (MORE durable than the ret_5m reversal's 58%).
  A slow gap-fade over the whole first hour → hold-window flexibility (don't have to unwind at 10:00).
- **Turnover:** ~1 rebalance/DAY (one open) — the lowest possible, the thing every 30m signal died on.

## The cost blocker (measured by the Lead at my flag)
The gap-fade must trade at/just-after 9:30 — the widest-spread minute. The existing cost view
common_spreads_at_cadence covers only 10:00-15:30, so it CANNOT gate this. Lead measured the open minute:
**half-spread 12.6bps @09:30, 6.7bps @09:35, 6.0bps @09:40** — 2-4× the 10:00 cost. So an IC −0.09 gap-fade
must clear ~6-12bps. Whether it can is the entire net-of-cost question; the signal is huge but the open is
cost-toxic (the open-minute analog of QA's 16:00 close toxicity). Lead is registering open-minute spreads
as a common_ table for shapes' gate.

## Handoff to explorer-shapes (shape #002)
The archaeology that should shape their gates:
1. **Target liq2/liq3**, not liq4 — the inverted-U means the most-liquid names have the weakest signal.
2. **Net-of-cost MUST use the open-minute spread** (6-12bps half), not the 10:00 cadence cost — else the
   verdict is falsely optimistic by 2-4×.
3. **A 10:00-entry variant** (skip the 9:30 spread, enter at 10:00 using the gap as the signal) exploits
   the 83% persistence to dodge the worst spread — worth a second arm. Trade-off: lose the first 30min of fade.
4. **Survivorship gate:** gap-fade is a TIMING signal (today's gap) → should survive per-symbol demean.
5. **Outlier/clustering:** gap days cluster on earnings/macro mornings — confirm the fade isn't driven by a
   few event days (the reversal outlier-day discipline applies).

## So what
This is the most promising shape the data lens surfaced: strongest signal, lowest turnover, durable. Its
fate hinges entirely on the open-minute execution cost — which is precisely why finding the cost blocker
(not just the signal) is the contribution. If shapes' net-of-open-cost test clears for liq2/liq3, this is a
real M3 candidate; if not, it's a documented "real signal, eaten by the open spread."
