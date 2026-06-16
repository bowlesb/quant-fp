# H9 Method

## Universe

- Top 300 symbols by median daily dollar-volume (close × volume), screened over a 10-date sample.
- Coverage filter: symbol must appear on ≥90% of the 50 selected dates (≥45 dates).
- Final universe: 300 symbols; representative names: MU, SPY, NVDA, QQQ, TSLA.

## Date Range

- 50 most recent trading dates: **2026-04-07 → 2026-06-16**.
- 2026-06-16 itself included in the universe scan but all bar partitions present; no empty-date exclusion needed (the date has bars).
- N.B.: timestamps in the parquet files are stored as ET clock times with a UTC label (i.e., "09:30 UTC" = 09:30 ET). All filters use the stored values directly.

## RTH Filter

- **Load window:** 09:30–18:00 ET (minutes 570–1080). The wider load window is required so that forward-return targets up to T+120 min past 15:50 ET can be resolved within the same calendar day.
- **Signal scoring:** only bars with T < 15:50 ET (minute < 950) are used as signal timestamps; bars in 15:50–18:00 ET exist only as forward-return targets.
- Extended-hours bars (pre-market < 09:30, after-hours > 18:00) are excluded entirely.

## Signal: vwap_dev_W

For each signal window W ∈ {30, 60}:

```
tvwap_W(T) = sum(close * volume over [T-W+1, T]) / sum(volume over [T-W+1, T])
vwap_dev_W(T) = close(T) / tvwap_W(T) - 1
```

Requires at least W consecutive 1-min bars within the same (symbol, date) partition. Computed using Polars `rolling_sum(..., min_samples=W).over(["symbol", "date"])`.

## Forward Return

```
fwd_ret_H(T) = close(T+H) / close(T) - 1
```

Matched by integer-minute join: close at minute `utc_minute + H` within the same (symbol, date). Null where the target minute has no bar (near EOD). H ∈ {60, 120}.

## Rebalance Cadence

Rebalance every H minutes starting from RTH open (09:30 ET = minute 570). Signal timestamps are snapped to the grid: `slot = floor((utc_minute - 570) / H)`, and only rows where `utc_minute == 570 + slot * H` are retained. This ensures each position is entered at most once per H-minute period and turnover is measured at the correct cadence.

## Decile L/S Construction

Within each (date, slot) cross-section:
- Rank all symbols by `vwap_dev_W` (ascending).
- Assign deciles 0–9 (0 = most negative = LONG leg, 9 = most positive = SHORT leg).
- Long return = mean fwd_ret_H for decile 0 names; Short return = mean fwd_ret_H for decile 9 names.
- L/S spread = long_ret − short_ret (reversion hypothesis: negative vwap_dev → positive future return).

## Turnover

Within each (symbol, date), compare a name's leg (L/S/neutral) between consecutive slots. Turnover = fraction of names that were in at least one leg and changed their leg assignment. Computed per period, averaged.

## Net Return

```
net_bps = gross_bps - turnover * RT_cost_bps
```

Three cost scenarios: RT = 4, 6, 10 bps. Cost anchor of 6 bps matches the liquid-tier measured spread from prior H1–H3 work.

## Canary (Null Distribution)

10 independent seeds. For each seed, within each (date, slot) cross-section, the forward-return column is shuffled using Polars `.shuffle(seed=s).over("_cs_key")`. The null L/S spread is computed identically. The 95th percentile of the null distribution (`canary_mean + 2 * canary_std`) defines the canary band.

## Statistical Test

Day-clustered t-statistic: mean L/S return is first averaged to one observation per calendar date, then a standard t-test over the 49-day series.
