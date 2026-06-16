# H10b Method: 8-K Drift Escalation

**Run date:** 2026-06-16  
**Script:** `experiments/2026-06-16-h10b-8k-drift-escalation/run_h10b.py`  
**Reuses:** H10 cohort/control/demean/canary infrastructure (`run_h10.py`)

## Walk-Forward OOS Split

- Full universe: 126 trading days (2025-12-15 to 2026-06-16)
- TRAIN: first 63 days (~2025-12-15 to ~2026-03-XX)
- OOS: last 63 days (~2026-03-XX to 2026-06-16)
- **Critical: per-symbol demean computed WITHIN each split.** A symbol's OOS demean mean uses only its OOS dates; zero cross-split leakage.
- KEEP bar: OOS demeaned t >= 2.0 at >= 1 of {1d, 3d, 5d}

## Item-Code / PEAD Split

- Source: SEC EDGAR submissions API (`data.sec.gov/submissions/CIK{cik:010d}.json`)
- The submissions JSON returns an `items` field per filing in the `filings.recent` block (e.g., "2.02,8.01").
- Sample: ~1,200 8-K filings (random seed 42) drawn from the full 8-K filing pool. One API call per unique CIK; recent filings (2025-2026) covered by the `recent` block without pagination.
- Rate limit: ~8 req/sec (0.12s sleep between CIK fetches), well under SEC's 10/sec limit.
- User-Agent: "research-bot ben.bowles@gmail.com" as required by SEC.
- Earnings 8-K: `"2.02" in items_str`
- Non-earnings 8-K: item codes parsed but 2.02 NOT present
- Filings not in the sample (accession not fetched) are excluded from this subset analysis.
- OOS demeaned drift computed separately for each subset.

## Survivorship Stress

- Liquid tertile: top 1/3 of symbols by **median daily dollar-volume** (close × volume, summed over RTH bars).
- Analysis restricted to this set of symbols; event cohort and control both filtered.
- OOS demeaned alpha + t computed within the liquid subset.

## Tradeable Entry Realism

- Entry price: **D+1 OPEN** (first bar with UTC time >= 13:30, i.e., the first minute bar at or after 09:30 ET / 13:30 UTC).
- Exit: close of D+h (unchanged from H10).
- Return formula: `close[t+h] / open_price[t] - 1`
- Cost deducted: 6 bps round-trip from event-side return only.
- Compared to H10's close-to-close entry for size-of-change assessment.

## Time Handling

- All `ts` in bars are genuine UTC. June dates are EDT (UTC−4), so 09:30 ET = 13:30 UTC.
- Open bar filter: UTC hour == 13 AND minute >= 30, OR UTC hour > 13 (first eligible bar of RTH).
- Close bar filter: last bar with UTC hour in [13, 21] (unchanged from H10).
- Verified: no ET/UTC confusion by printing first/last RTH bars during H10 run.

## Canary

- 10-seed within-date permutation canary on every analysis cell (same as H10).
- All cells require canary p95 < alpha to claim "clears canary."
