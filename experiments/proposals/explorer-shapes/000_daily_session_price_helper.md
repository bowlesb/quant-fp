# Proposal 000 — Daily session-price helper table (UNBLOCKER, not a shape)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)

## What this is
NOT a strategy shape — the cheap derived artifact that unblocks the entire open-anchored shape
CLASS (proposals 002, 004, and gap-conditioning used elsewhere). Prior session DEFERRED Shapes
1+2 *purely* on bar-scan cost and named exactly this fix in the journal.

## The problem it solves
Open-anchored shapes need, per (symbol, trading-date): the 09:30 RTH open price, the 10:00
price, and the 16:00 close. Today that means scanning `bars_1m` with
`(ts AT TIME ZONE 'ET')::time IN ('09:30','10:00','16:00')` — a **non-indexable** predicate that
scans all 693 monthly chunks even for 3 minutes/day. With FILTER+GROUP BY it ran 5+ min per
experiment and contended with prod's post-close batch. Re-scanning per experiment does not scale
to Ben's "more strategies" standing order.

## Spec
Materialize ONCE into the sandbox (read-only on bars_1m; coordinate the write target with prod):

```
daily_session_price(symbol TEXT, trade_date DATE,
                    open_0930 DOUBLE PRECISION,   -- close of the 09:30 ET RTH open bar
                    px_1000   DOUBLE PRECISION,   -- close of the 10:00 ET bar
                    high_0930_1000 DOUBLE PRECISION, low_0930_1000 DOUBLE PRECISION, -- first-30-min range
                    vol_0930_1000  DOUBLE PRECISION,  -- first-30-min volume (for ORB / gap conditioning)
                    close_1600 DOUBLE PRECISION,  -- close of the 15:59 ET bar (canonical close)
                    PRIMARY KEY (symbol, trade_date))
```
- Source: `bars_1m WHERE source='backfill'` (research basis), RTH only, ET calendar.
- ONE sequential scan over bars_1m builds the whole table (group by symbol, date with conditional
  aggregation on the minute) — pay the 693-chunk scan ONCE, not per experiment.
- PIT note: this is a pure deterministic function of stored bars; no future leakage concern (it's
  same-day prices). Open-anchored LABELS built FROM it (open->close) are forward returns and live
  in the labels layer, not here.

## Why now / cheapness
★ — one-time read over existing bars. Fits task #22's composable-materialization vision exactly
(the modeller flagged this as the archetypal derived artifact). Run when the DB is quiet (post-batch).

## Gates (this is infra, not an experiment — but acceptance checks apply)
- **Acceptance:** row count ≈ (n trading dates) × (avg universe/date); spot-check 5 (symbol,date)
  rows against a direct bars_1m query (open/10:00/close must match to the cent).
- **Calendar/DST:** 09:30/10:00/16:00 resolved in ET with DST handled (reuse the calendar util the
  panel builder uses — do NOT hardcode UTC offsets).
- **No half-sessions silently wrong:** early-close days (16:00 bar absent) must be flagged/NULL, not
  filled with a stale price.

## Lead disposition
<!-- Lead fills: validated? duplicate? data exists? enqueued? -->
