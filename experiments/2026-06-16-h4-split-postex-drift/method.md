# H4 Method: Split POST-event Drift (reverse vs forward, liquid-tertile gate PRIMARY)

**Run date:** 2026-06-16
**Pre-registration:** `experiments/2026-06-16-h4-split-postex-drift/hypothesis.md`
**Script:** `run_h4.py`

## Data sources

**Corporate actions:** `corporate_actions_pit` view in timescaledb.
Columns: `symbol`, `ex_date`, `action_type`, `split_ratio`.
Filter: `action_type = 'split'` AND `ex_date >= 2025-12-15`.
Direction: `split_ratio < 1` → reverse split (price decreases; distress signal).
Direction: `split_ratio >= 1` → forward split (price increases; attention signal).
Tested SEPARATELY. Pooling cancels opposite signs.

Total in bars window: 449 splits (406 reverse, 43 forward).
After entry-date assignment: 404 reverse (2 had no next trading day), 43 forward.
After filtering to symbols with bar data: **312 reverse, 17 forward**.

**Bars:** `/store/raw/bars/symbol=<S>/date=<D>/data.parquet` — minute OHLCV, genuine UTC.
7,671 symbol directories; 7,337 with RTH bars; 831,669 (symbol, date) rows; 126 trading dates.

## UTC time handling

All bar timestamps are genuine UTC (per RESEARCH_PITFALLS.md — no off-by-240 bug).
- EST (Dec–Mar): market opens 14:30 UTC = 09:30 ET.
- EDT (Mar–Nov): market opens 13:30 UTC = 09:30 ET.

RTH filter: UTC hour in [13, 21] inclusive (safely captures both EST and EDT sessions).
Open price: FIRST bar in the RTH-filtered set per date (handles both EST and EDT open times).
Close price: LAST bar in the RTH-filtered set per date.

Verified: sample path `/store/raw/bars/symbol=IIF/date=2026-01-14/data.parquet` shows
first bar at 14:30 UTC (January = EST). Our "first RTH bar" approach correctly captures this.

## Event flag and entry date assignment

Event = ex_date of split (from `corporate_actions_pit`). ex_date is look-ahead safe.
**Entry date = next trading day after ex_date** (D+1 open; conservative, never same-day).
Entry PRICE = open_price on entry date (tradeable: entering at open, not the ex_date close).
Forward returns: fwd_h = close[entry + h trading days] / open[entry] - 1.
Horizons tested: {1, 3, 5, 10, 20} trading days.

## Liquidity tertile assignment

Symbol-level liquidity proxy: median daily dollar volume (sum of close*volume per bar per day)
across the full 126-day bars window.
Tertile thresholds (7337 symbols, ~2445 per tier):
- Tier 1 (illiquid): median dvol <= $208,739/day
- Tier 2 (middle): $208,739 < median dvol < $9,283,830/day
- Tier 3 (liquid): median dvol >= $9,283,830/day

Tier assignment is symbol-level (static across time), not date-varying.

## Control matching

Per-date cross-sectional comparison. For each event date d:
- Event cohort: symbols entering on date d (with valid fwd_h return).
- Control cohort: all other symbols with valid fwd_h on date d.
- Date alpha: mean(event returns on d) − mean(control returns on d).

Controls and events within the same tier for tier-specific analysis.

## Metrics

1. **Overall alpha:** mean of date alphas across event dates.
2. **Day-clustered t-statistic:** alpha_mean / (std(alpha_d) / sqrt(n_dates)).
3. **10-seed shuffle canary:** permute event flag within each date; canary_p5 / canary_p95.
   Signal clears canary if: positive alpha > canary_p95, or negative alpha < canary_p5.
4. **Per-symbol-demean:** subtract each symbol's own mean forward return across all dates.
   Tests whether signal survives removing symbol-level idiosyncratic drift.

## Caveats

- **Survivorship:** only symbols in the live bars store; delisted names absent.
- **Open filter bug (v1):** initial run used hour==13 AND minute>=30 for open — this missed
  EST-period opens (14:30 UTC). Fixed in v2: use FIRST RTH bar regardless of hour.
  Results reported are from the corrected v2 run.
- **Forward split N:** only 17 forward splits matched bars, 16 with valid open prices.
  ALL cells for forward splits are underpowered (<20 events). Cannot distinguish signal
  from noise; every result here is directionally suggestive only.
- **Reverse split liquid N:** only 4 events in liquid tier. Strongly directional but
  underpowered by pre-commitment threshold.
