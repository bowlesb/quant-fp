# H11 first run — INVALIDATED (off-by-240 timezone bug)

**2026-06-16.** The first H11 run (`raw_results.json`) reported a +12 to +34 bps net-of-cost longer-horizon
momentum "edge" that appeared to survive the cost gate, canary, and per-symbol-demean. It is a **SUSPECTED
ARTIFACT** and is invalidated. The MA caught it during pre-KEEP verification.

## The bug (proven against the data)

`run_h11.py` computes `utc_minute = ts.hour*60 + ts.minute` and uses RTH constants
`RTH_START_UTC=570 (09:30), RTH_TRADEABLE=575 (09:35), RTH_END_UTC=950 (15:50)` — assuming `ts` is
ET-labelled. But the `/store/raw/bars` `ts` is **genuine UTC** (verified: a 13:30 UTC bar is 09:30 ET; an
08:00 UTC bar is 04:00 ET premarket). So 09:30 ET is really `utc_minute = 13*60+30 = 810`, not 570 — every
constant is off by +240 minutes.

Consequences:
1. The entry slot grid `570 + slot*H` lands real entries at `utc_minute` 810/870/930 = 09:30/10:30/11:30 ET.
   **810 IS the 09:30 OPEN PRINT.**
2. The "tradeable entry" GATE A filter `utc_minute >= 575` keeps EVERY entry (all real bars are >=810), so it
   removes nothing → `tradeable_entry == raw` in every cell. **The gate that should exclude the 09:30 open
   NEVER FIRES.**
3. So the momentum L/S is anchored to entering at the 09:30 open you can't actually trade — the platform's
   #1 false-edge trap (open-anchored / non-tradeable entry; the same shape that killed the gap-fade).
4. `RTH_END_UTC=950` also meant scoring only 09:30–11:49 ET (the first ~2.3h), not the full session.

## Lesson

The `tradeable_entry == raw` identity was THE tell — an entry gate that changes nothing is far more likely a
no-op bug than a lucky no-op. The verification (read the gate code → re-derive the actual entry minute against
the real timestamps) is what caught it. This is the tradeable-entry discipline working exactly as intended.

## Re-run

Corrected run pending: UTC-correct constants (09:30 ET=810, 09:35 ET=815, 15:50 ET=1190), entry = the FIRST
tradeable bar at/after 09:35 ET (utc_minute>=815), never the 810 open print; raw-vs-tradeable must now differ;
one cell hand-verified (entry timestamp + price). Prior: the edge shrinks or vanishes once the open print is
genuinely excluded. The corrected verdict supersedes this file.
