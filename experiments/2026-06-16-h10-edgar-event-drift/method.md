# H10 Method: EDGAR 8-K / Form-4 Event Drift

**Run date:** 2026-06-16  
**Pre-registration:** `experiments/2026-06-16-event-families-pivot/hypothesis.md` H10 section

## Data sources

**Filings:** `filings` table in timescaledb. Form types: `8-K` and `4` (Form-4).  
Columns used: `symbol`, `form_type`, `available_at` (UTC, look-ahead-safe acceptance time),
`available_at_source`.  
Window: `available_at >= 2025-12-15`.

All 12,975 matched 8-K filings and 48,986 Form-4 filings had
`available_at_source = 'submissions_accepted'` (SEC acceptance time, backfill quality — fine
for a daily-horizon study where intraday timestamp precision does not matter).

**Bars:** `/store/raw/bars/symbol=<S>/date=<D>/data.parquet` — minute OHLCV in genuine UTC.
7,671 symbol directories; 7,337 had at least one RTH bar.  
Universe: 831,669 (symbol, date) observations across 126 trading dates
(2025-12-15 to 2026-06-16).

## UTC time handling

All timestamps are genuine UTC. Verified by inspection of sample bars:

- 14:30 UTC = 09:30 ET (EDT, summer) = market open
- 21:00 UTC = 16:00 ET (EDT) = market close (approximately)
- In winter (EST), 13:30 UTC = 08:30 ET (pre-open); 20:00 UTC = 15:00 ET

RTH filter applied: **UTC hour in [13, 21] inclusive**. This safely captures the full
regular-hours session under both EDT and EST without truncating any RTH bars.

Daily close price: the **last bar's close** within the RTH filter for each (symbol, date).

## Event flag and entry date assignment

For each filing with `available_at >= 2025-12-15`:

1. Compute `available_at_date = available_at::date` (UTC date).
2. **Entry date = next trading day after `available_at_date`** (conservative: never
   same-day entry, regardless of whether the filing was pre-market or after-hours).
3. A trading day is any date present in the bars panel for at least one symbol.
4. The entry price is the close of the entry date (position entered at/before close,
   forward return measured close-to-close).

Filings that overlap — same (symbol, entry_date) from multiple filings — are deduplicated
(treated as one event day per symbol).

Filings for symbols with no bar data are dropped (~2 symbols lost).

Final counts after filtering and deduplication:
- 8-K: 12,868 filings, 2,136 symbols
- Form-4: 47,456 filings, 2,150 symbols

(Note: these are higher than the pre-registered 1,973 / 7,831 because the earlier count
used a stricter `available_at >= 2025-12-15` with the old backfill state; the table has
grown since.)

## Forward returns

For each (symbol, date) pair in the close panel, forward return at horizon h is:

    fwd_h = close[t + h trading days] / close[t] - 1

where t+h is the h-th next date with a bar for that symbol (no calendar-day arithmetic
— strictly trading-day shifts using polars `.shift(-h).over("symbol")`).

Horizons: 1, 3, 5, 10 trading days.

## Control matching

Per-date cross-sectional comparison. For each calendar date d with at least one event entry:

- **Event returns:** forward returns of event symbols entering on date d.
- **Control returns:** forward returns of all other symbols in the universe on date d
  (any symbol with a valid forward return for that date that is NOT an event symbol on d).
- **Date alpha:** mean(event returns on d) - mean(control returns on d).

Market moves cancel by construction because event and control symbols share the same date.

## Metrics

1. **Overall alpha:** mean of date alphas across all event dates.
2. **Day-clustered t-statistic:** alpha_mean / (std(alpha_d) / sqrt(n_dates)).
   Treats each event date as one observation — accounts for cross-sectional correlation.
3. **10-seed shuffle canary:** permute the event flag within each date (random shuffle of
   all symbols on the date; the first n_events_today become "event", the rest "control").
   Repeat 10 times with different seeds. Report canary_mean, canary_p5, canary_p95.
   The real signal is compared against these bounds.
4. **Per-symbol-demean:** subtract each symbol's own mean forward return (across all dates
   in the panel) before computing cross-sectional alpha. Tests whether the event signal
   survives removing symbol-level idiosyncratic drift (e.g., a persistently trending stock
   with many filings inflating the event cohort).

## Caveats

- **Survivorship:** universe = symbols currently in the bars store. Delisted names absent.
- **Timestamp quality:** all `available_at_source = 'submissions_accepted'` (SEC acceptance
  time, not live-feed time). Likely within minutes to an hour of actual public availability.
  Appropriate for daily-horizon studies.
- **Form-4 direction:** no buy/sell direction column — this is "insider-ACTIVITY day," not
  "insider-BUY day." A direction split would require parsing the raw XML index.
- **8-K item code:** no item-code granularity — all 8-K events pooled. A 2.02 (earnings)
  vs 5.02 (management change) split would require XML parsing.
- **Entry timing:** we use close-to-close with a D+1 entry rule. A live implementation
  would enter at D+1 open; the open-vs-close difference is a real-world slippage source
  not captured here.
- **Universe size:** varies by date (not all 7,337 symbols have bars every day).
