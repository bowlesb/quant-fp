# W3 — Method: 13D activist-stake drift on LIQUID targets

Pre-registered in `hypothesis.md` (2026-06-16, BEFORE running; Lens L5 — EDGAR fundamentals CONTENT).
This file documents the exact mechanics; `results.md` has the tables; `verdict.md` the decision.
The 13F institutional-holdings sub-test (hypothesis §2) requires parsing the 13F XML information table
per target and is DEFERRED to a follow-up — this run is the cleaner, higher-signal 13D event alone.

## 1. Event identification (13D activist filings)
- Pull from the `filings` table (PIT, look-ahead-safe `available_at` in genuine UTC):
  `form_type IN ('SC 13D','SC 13D/A','SCHEDULE 13D','SCHEDULE 13D/A')`, `symbol IS NOT NULL`,
  `available_at >= '2024-12-11'` (the bars window start). De-duplicated on
  `(symbol, available_at_date, accession_number)`.
- **INITIAL 13D** = `SCHEDULE 13D` / `SC 13D` (a NEW >5% activist stake declared — the clean info
  shock). **AMENDMENT 13D/A** = `.../A` (stake/intent change). Reported SEPARATELY and combined.
- `available_at` is the PIT event timestamp (day-correct for backfill rows), so the D+1-open
  multi-day entry is look-ahead-safe (RESEARCH_PITFALLS #3 note on PIT event time).

## 2. Universe + liquidity tier (PRIMARY gate)
- Daily (symbol, date) panel from `/store/raw/bars` minute bars (read-only mount), reusing the W2
  cached panel (identical bars and 2024-12-11..2026-06-16 window). Per (symbol, date): `close` = last
  RTH bar; `open_price` = first RTH bar at/after 13:30 UTC (= 09:30 ET, summer); `dollar_vol` =
  sum(close*volume) over RTH bars. RTH = UTC hour in [13,21]; minute math cast to **Int32**
  (RESEARCH_PITFALLS #1 — bars `ts` is genuine UTC, 13:30 UTC = 09:30 ET; never read ET minutes off a
  UTC stamp).
- `adv_dollar` = median daily dollar-volume per symbol over the last 20 trading days.
- **LIQUID tier = top tertile by `adv_dollar`** (the H10b lesson: pooled-event drift lives in the
  illiquid tail and dies in liquid names — so the LIQUID tier is the PRIMARY report). A **top-300**
  sub-cut is reported as a robustness slice; mid/illiquid tertiles + full universe are context.

## 3. Entry, forward returns
- **Entry = D+1 OPEN** after `available_at` (the next trading day's open; tradeable, never the filing
  instant; if `available_at` lands on a non-trading day, the next trading day's open is used).
- Forward returns open-entry → close at horizons {1, 3, 5, 10, 20, 40, 60} trading days (activist
  drift is slow — extended to a quarter): `open_fwd_Hd = close[t+H] / open_price[entry] - 1`.
- Every forward-return column is FINITE-FILTERED with `.is_finite()` (RESEARCH_PITFALLS #10 —
  `drop_nulls` does NOT drop the trailing-NaN forward returns).

## 4. Portfolio construction — directional LONG cohort
13D activist drift is a DOCUMENTED POSITIVE-direction event (Brav-Jiang-Partnoy-Thomas), so the test
is a directional **LONG the 13D cohort**, not a signed-by-reaction L/S.
- **Headline drift:** equal-weight the LIQUID 13D cohort's open-entry forward return at each horizon,
  minus the same-date non-event control mean, **per-symbol-demeaned**, **day-clustered** (one
  observation per event date — RESEARCH_PITFALLS #5 clustering unit is the DAY, never the cell).
- **Per-trade tradeable bet:** each 13D event = one realized LONG round-trip (enter D+1 open, hold H
  days, exit). Per-trade net = (event open-fwd return) − (same-date control mean) − (round-trip cost).
  Events are NON-OVERLAPPING units (RESEARCH_PITFALLS #6 — bootstrap the realized round-trips, never an
  overlapping IC-weighted average).
- **Net of cost:** deduct the measured LIQUID round-trip cost (median liquid half-spread ×2) and a 2×
  stress. Liquid half-spreads measured from `/store/raw/quotes` (lazy per-symbol, last 5 days).

## 5. Gates
- **Shuffle-canary:** within each event date, permute event/control labels (20 seeds); the canary
  alpha must sit at ~0 with the real alpha well outside its p95.
- **Per-symbol demean:** subtract each symbol's own mean forward return (null-safe group-mean join)
  before forming cohort-minus-control — kills static per-name level effects (RESEARCH_PITFALLS #3).
- **Walk-forward OOS:** trading days split in half by date. UNLIKE W2 (whose 8-Ks were all in the OOS
  half), 13D filings span the full window (~250–440/month across 2024-12..2026-06), so BOTH halves
  carry events — a genuine TRAIN/OOS event study with no fitted parameters.
- **Per-trade bootstrap:** 10k resamples of the realized D+1→D+H LONG round-trips, net of cost; KEEP
  requires the bootstrap CI to **exclude zero above** (lower bound > 0) on the LIQUID OOS leg.
- **Cost gate:** measured liquid spread, and spread×2 stress; net drift must survive.

## 6. Decisiveness
DECISIVE = LIQUID OOS net-of-cost long drift with per-trade bootstrap CI lower bound > 0,
demean-surviving, canary-clean, on adequate N. Pre-committed N guard: < ~30 liquid events in a cell =
"needs more history", NOT a verdict. KILL = no drift beyond canary, OR net ≤ 0 with adequate N.

VECTORIZED throughout (polars panel ops + numpy bootstrap). Metric helpers in the sibling
`hf_metrics_fixed.py`; harness adapted from the W2-PEAD event-study pipeline.
