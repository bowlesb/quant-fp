# H5 Method: Dividend POST-EX Drift

**Run date:** 2026-06-16  
**Script:** `run_h5.py` (exit code 0)

## Event Definition

- Source: `corporate_actions_pit` VIEW in TimescaleDB, filtered to `action_type = 'cash_dividend'`
- Event date = `ex_date` (a DATE column — no UTC conversion needed; data revealed on ex_date)
- Window: ex_date >= 2025-12-14 AND ex_date <= 2026-06-15

**Look-ahead safety:** ex_date is look-ahead-safe by design — the corporate action is public knowledge as of the ex_date. We still never trade on the ex_date itself (see entry rule below).

## Entry Rule (D+1 Open — Tradeable Entry)

Entry date = the FIRST TRADING DAY AFTER ex_date. This guarantees:
- No same-day open print (the open on ex_date itself — where the stock opens ex-dividend — is excluded)
- Tradeable: the 09:30 ET open on D+1, filled at the first RTH bar

Entry price = `open_price` = first bar on the entry date where UTC timestamp satisfies:
- `(utc_hour == 13 AND utc_minute >= 30) OR utc_hour > 13`
- This correctly captures the 09:30 ET open in both EDT (UTC-4, summer): 13:30 UTC and EST (UTC-5, winter): 14:30 UTC

## UTC Timestamp Handling

CRITICAL: bars `ts` column is genuine UTC. 09:30 ET (EDT) = 13:30 UTC. The UTC verification at runtime confirmed:
- First RTH bar: 14:30 UTC on a January date (EST: 09:30 ET = 14:30 UTC) — correct
- Last RTH bar: 21:00 UTC (EST: 16:00 ET = 21:00 UTC) — correct

We NEVER assume a fixed UTC offset: we use `utc_hour >= 13 AND utc_hour <= 21` for RTH, which covers both EDT (open at 13:30 UTC) and EST (open at 14:30 UTC) without hardcoding an offset.

Per RESEARCH_PITFALLS.md Rule #1: off-by-240 bug was fatal in H11 (open constant wrong by 4 hours). We verified the open filter fires correctly by inspection.

## Forward Return Definition

```
open_fwd_h = close[D+1 + h sessions] / open_price[D+1] - 1
```

- Entry at D+1 OPEN (tradeable, never the ex-date print)
- Exit at RTH close h trading sessions after entry
- Horizons: {1, 3, 5, 10} trading days
- Cost: 6 bps round-trip deducted from event-side return (net-of-cost)

## Universe and Liquid Tertile

- Universe: 7,337 symbols with at least one bar in the 2025-12-15 to 2026-06-16 window (126 trading dates)
- Liquid tertile: top 1/3 of symbols by **median daily dollar-volume** (= `sum(close * volume)` over RTH bars per day, then median across all dates). Threshold: 2,445 / 7,337 symbols.
- PRIMARY gate: liquid-tertile OOS demeaned t. Full-universe is secondary context.

## Cohort vs Control Design

- Per trading date: dividend-ex cohort (symbols with entry_date = that date) vs all other symbols in the universe (non-event controls) on the same date
- Per-symbol demean: subtract each symbol's own mean forward return computed WITHIN the split only — no cross-split leakage
- Day-clustered t-statistic: `mean(alpha_per_date) / (std(alpha_per_date) / sqrt(n_dates))`

## Walk-forward OOS Split

- TRAIN: 2025-12-15 to 2026-03-17 (63 trading days)
- OOS: 2026-03-18 to 2026-06-16 (63 trading days)
- Per-symbol demean computed INDEPENDENTLY within each split — zero cross-split contamination
- PRIMARY headline = OOS result on liquid tertile

## 10-Seed Shuffle Canary

Within each date, permute the event flag (shuffle all returns, assign first N to "event"), compute same cross-sectional alpha, repeat 10 seeds. Canary p95 = 95th percentile of the 10 permuted alphas. Signal must clear the canary (actual alpha > canary_p95) to be meaningful.

## Dividend Yield Split

- Compute yield = `cash_amount / close_on_ex_date` for each event
- Split into terciles (low/mid/high) by yield value across all in-panel events
- Run liquid-tertile + full-universe OOS for each tercile separately
- Purpose: check if drift concentrates in high-yield payers (and whether those are liquid)

## Net-of-Cost Convention

- Round-trip cost = 6 bps = 0.0006 in decimal
- Applied to event-side return only: `event_return_net = event_return - 0.0006`
- Consistent with H10b convention
