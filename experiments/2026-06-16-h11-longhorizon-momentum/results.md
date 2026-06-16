# H11 Results (v2 — timezone-corrected)

**CRITICAL NOTE: v1 results were INVALID due to an off-by-240-minute timezone bug.**
The bars `ts` is genuine UTC. H9/H11-v1 used ET-labeled constants (RTH_START=570 = 09:30 ET)
as UTC minutes, placing entries at 05:30-11:50 UTC (pre-dawn / early morning), with slot-0
at 13:30 UTC = 09:30 ET = the open print, and Gate A at 09:35 UTC (5:35 AM ET) never firing.
v2 corrects all constants: RTH_START=810 (13:30 UTC=09:30 ET), RTH_END=1190 (19:50 UTC=15:50 ET),
RTH_TRADEABLE=815 (13:35 UTC=09:35 ET).

**300 liquid symbols × 49 dates (2026-04-07 to 2026-06-16)**

## 1. Slot Grid (after timezone fix + W-bar lookback)

For W=30, min valid vwap_dev is at bar 810+29 = 839. First rebalance slot with valid signals:
- H=60: slots at 870, 930, 990, 1050, 1110, 1170 (10:30–19:30 ET/UTC)
- H=120: slots at 930, 1050, 1170 (11:30, 13:30, 15:30 ET)

For W=60, first valid vwap_dev at bar 810+59 = 869. First valid slot:
- H=60: slots at 870, 930, 990, 1050, 1110, 1170
- H=120: slots at 930, 1050, 1170

Slot-0 at minute 810 (09:30 ET open print) NEVER appears in the panel — null vwap_dev.
Gate A (>=815) removes 0 rows, identical to raw. The open-print trap structurally does not apply.

## 2. Raw Momentum (Standard Entry, Full RTH)

| W  | H   | Gross (bps) | Turnover | Net@4bps | Net@6bps | Net@10bps | t-stat (day-clust) | Canary 95th | Clears Canary |
|----|-----|-------------|----------|----------|----------|-----------|-------------------|-------------|---------------|
| 30 |  60 |     +7.53   |   0.894  |   +3.95  |   +2.16  |   −1.41   |       1.42        |    +4.58    |     YES       |
| 30 | 120 |    +15.90   |   0.895  |  +12.32  |  +10.53  |   +6.95   |       1.64        |   +12.97    |     YES       |
| 60 |  60 |     +3.42   |   0.891  |   −0.15  |   −1.93  |   −5.50   |       0.64        |    +7.33    |     NO        |
| 60 | 120 |    +13.46   |   0.894  |   +9.88  |   +8.09  |   +4.51   |       1.18        |   +13.53    |     NO        |

Only W30 cells clear canary in the full-session raw view.

## 3. Gate A: Tradeable Entry (>=09:35 ET = >=13:35 UTC = >=815)

IDENTICAL to raw in all cells. The 09:30 ET slot never has valid vwap_dev (W-bar lookback
from RTH start requires W bars before signal is valid, and bars start at 09:30 ET).
The open-print contamination is structurally absent from this signal.

## 4. Gate B: Per-Symbol Demean (Survivorship/Idiosyncratic Check)

| W  | H   | Demeaned Gross | Demeaned Net@6 | t-stat | Clears Canary |
|----|-----|----------------|----------------|--------|---------------|
| 30 |  60 |      +6.20     |     +0.84      |  1.17  |     YES       |
| 30 | 120 |     +14.47     |     +9.10      |  1.51  |     YES       |
| 60 |  60 |      +1.98     |     −3.37      |  0.37  |     NO        |
| 60 | 120 |     +11.20     |     +5.83      |  1.00  |     NO        |

Signal survives demean with small decay (~1.2–2.3 bps reduction in gross).
Not a survivorship/idiosyncratic artifact.

## 5. Combined: Tradeable Entry + Per-Symbol Demean

| W  | H   | Gross  | Net@6  | t-stat | Clears Canary |
|----|-----|--------|--------|--------|---------------|
| 30 |  60 | +6.20  | +0.84  |  1.17  |     YES       |
| 30 | 120 | +14.47 | +9.10  |  1.51  |     YES       |
| 60 |  60 | +1.98  | −3.37  |  0.37  |     NO        |
| 60 | 120 | +11.20 | +5.83  |  1.00  |     NO        |

## 6. Robustness: Exclude First + Last 30 Min (10:00–15:30 ET = 14:00–19:30 UTC)

| W  | H   | Gross (bps) | Net@6bps | t-stat | Notes |
|----|-----|-------------|----------|--------|-------|
| 30 |  60 |     +6.94   |   +1.58  |  1.36  | just clears canary (95th=1.65) |
| 30 | 120 |    +20.46   |  +15.07  |  2.82  | strong; slots 930+1050 only    |
| 60 |  60 |     +5.06   |   −0.28  |  1.08  | borderline                     |
| 60 | 120 |    +25.53   |  +20.14  |  3.27  | strongest cell overall         |

The mid-session signal (10:00–15:30 ET) is STRONGER than the full-session signal,
especially at H=120. The t-stats rise sharply when excluding the noisy early/late session.
W60/H120 robust: +25.53 bps gross, +20.14 net@6, t=3.27 — clears canary (95th=5.92).

## Key Summary by Gate

| Cell     | Raw Gross | Raw Net@6 | t-stat | Tradeable Net@6 | Demean Net@6 | Robust Net@6 | Final Canary Pass |
|----------|-----------|-----------|--------|-----------------|--------------|--------------|-------------------|
| W30/H60  |   +7.53   |   +2.16   |  1.42  |     +2.16       |    +0.84     |    +1.58     | YES (barely)      |
| W30/H120 |  +15.90   |  +10.53   |  1.64  |    +10.53       |    +9.10     |   +15.07     | YES               |
| W60/H60  |   +3.42   |   −1.93   |  0.64  |     −1.93       |    −3.37     |    −0.28     | NO (raw fails)    |
| W60/H120 |  +13.46   |   +8.09   |  1.18  |     +8.09       |    +5.83     |   +20.14     | NO (raw fails)    |

The robust W60/H120 result is intriguing (+25.53, t=3.27) but it only passes canary in the
narrow mid-session window and fails canary in the full-session raw view.

## Comparison: v1 (invalid, wrong TZ) vs v2 (corrected)

| Cell     | v1 Gross (WRONG) | v2 Gross (CORRECT) | Drop |
|----------|-----------------|-------------------|------|
| W30/H60  |    +12.68       |     +7.53         | −5.2 bps |
| W30/H120 |    +28.47       |    +15.90         | −12.6 bps |
| W60/H60  |    +24.66       |     +3.42         | −21.2 bps |
| W60/H120 |    +33.67       |    +13.46         | −20.2 bps |

The timezone bug inflated gross figures by 5–21 bps. The W60 cells went from clearly positive
to near-zero/failing canary.
