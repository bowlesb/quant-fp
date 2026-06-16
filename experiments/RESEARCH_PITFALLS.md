# Research pitfalls — methodology notes for explorers (read before writing a panel script)

Append-only. Each entry is a concrete bug that produced (or nearly produced) a false result, and the rule
that prevents it. Single-writer = the MA.

## 1. UTC-vs-ET off-by-240 — the PARITY-INVISIBLE timezone trap (2026-06-16, H11)

**What happened.** An H11 explorer built session-time logic with `utc_minute = ts.hour()*60 + ts.minute()`
and constants `09:30 ET = minute 570`. But `/store/raw/bars` `ts` is **genuine UTC** (13:30 UTC = 09:30 ET).
So 09:30 ET is really minute **810**, not 570 — every constant was off by +240. The entry grid landed on the
09:30 OPEN PRINT, and a `>=09:35` "tradeable-entry" gate (575) became a NO-OP (every real bar is >=810). The
result was a +28 bps "momentum edge" that was actually an open-anchored artifact. Caught only because the
gate's output was identical to the ungated output (the tell), and a hand re-derivation of the entry minute
exposed the off-by-240.

**Why it is dangerous.** This bug class is PARITY-INVISIBLE: if it crept into a PRODUCTION session-time
feature, live and backfill would both be wrong the SAME way and MATCH each other, so the
compute==compute_latest parity gate would never catch it. (It would still be caught by golden-set validation
of the calendar features, and by the no-look-ahead test for the entry-anchoring half — but the raw
time-of-day error itself is silent to parity.)

**The rule.**
- NEVER compute ET session minutes by reading `.hour()`/`.minute()` off a UTC timestamp. ALWAYS convert
  first: `ts.dt.convert_time_zone("America/New_York")` (DST-aware — June is EDT = UTC−4, but don't hardcode
  the offset; let the tz database handle DST). This is what production `quantlib/features/groups/calendar.py`
  does correctly (and it even comments the Int8 `hour()*60` overflow trap — cast to Int32 first).
- VERIFY your RTH filter against real bars before trusting it: print a few `ts` and the derived session
  minute; confirm 13:30 UTC maps to "market open" and 20:00 UTC to "close".
- A tradeable-entry gate that does not CHANGE the result is a no-op bug until proven otherwise. An entry gate
  is supposed to exclude the open print; if raw == gated, the gate didn't fire — investigate before trusting.

## 2. The tradeable-entry trap (standing, pre-2026-06-16)

A return must be booked from a TRADEABLE entry price (≥09:35 ET, never the 09:30 print) and cost must be the
MEASURED open spread — not a flat charge on a 09:30 return. Open-anchored labels / 09:30-print fills are the
platform's #1 false-edge source (killed the gap-fade, open-cadence gap, open-anchored momentum). Pitfall #1
above is a NEW way to accidentally re-introduce this (a broken time grid silently re-anchors to the open).

## 3. Survivorship / per-symbol-demean (standing)

Any cross-sectional "edge" must survive a per-symbol demean (subtract each symbol's own mean forward return).
The overnight "edge" was survivorship and collapsed under demean. Run the demean gate on every cohort/L-S
result; an edge that vanishes under demean is an idiosyncratic/survivorship artifact, not alpha.
