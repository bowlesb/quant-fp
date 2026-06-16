# Method: H2-RETEST OFI Orthogonal to vwap_dev

## Symbol Selection

1. Collect all symbols present in `/store/raw/quotes/` (2,504 liquid symbols).
2. From `/store/raw/bars/`, compute median daily dollar volume (close × volume) across all available
   bar dates for those symbols.
3. Select the top 250 by median daily dollar volume that also have trades data. If fewer than 150
   pass quality filters (≥10 days of data), adjust threshold.
4. Always include: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, BRK.B, JPM, SPY, QQQ (force-include
   megacaps even if they rank just outside top 250 by some metric).

## Date Range

- Completed days only: 2026-05-18 through 2026-06-15 (exclude 2026-06-16, today/partial).
- Expect ~20 trading days (21 directory dates minus today).

## Regular Trading Hours Filter

- Keep minutes where ts (UTC) is in [13:30, 19:50).
  - 13:30 UTC = 09:30 ET (RTH open)
  - 19:50 UTC = 15:50 ET (exclude last 10 min pre-MOC)
- All timestamps are assumed UTC.

## OFI Computation (Cont-Kukanov-Stoikov)

For each symbol-day, process quote updates chronologically:

Per consecutive quote update pair (prev → curr):

```
bid_e =  curr.bid_size                      if curr.bid_price > prev.bid_price
       = curr.bid_size - prev.bid_size      if curr.bid_price == prev.bid_price
       = -prev.bid_size                     if curr.bid_price < prev.bid_price

ask_e = -prev.ask_size                      if curr.ask_price < prev.ask_price
       = curr.ask_size - prev.ask_size      if curr.ask_price == prev.ask_price
       =  curr.ask_size                     if curr.ask_price > prev.ask_price

ofi_event = bid_e - ask_e
```

Aggregate ofi_event per minute (floor ts to minute) → `ofi_1m`.

Rolling window (within RTH, per symbol-day):
- `ofi_15 = sum(ofi_1m, 15 periods)`
- `ofi_30 = sum(ofi_1m, 30 periods)`
- `total_vol_15 = sum(minute_quote_count, 15 periods)` (proxy for total activity)
- `ofi_15_norm = ofi_15 / (total_vol_15 + 1e-9)` (normalized by quote event count)
- `ofi_30_norm = ofi_30 / (sum(quote_count, 30) + 1e-9)`

## Signed Volume (Tick-Rule)

For each symbol-day, process trades chronologically:

```
direction = +1 if price > prev_price (uptick)
           = -1 if price < prev_price (downtick)
           = prev_direction if price == prev_price (carry)
```

Initial direction for first trade of day: +1 (arbitrary, washed by cross-section demeaning).

Per minute: `buy_vol = sum(size where direction=+1)`, `sell_vol = sum(size where direction=-1)`.
`sv_1m = buy_vol - sell_vol`.

Rolling:
- `sv_15 = sum(sv_1m, 15)`
- `sv_30 = sum(sv_1m, 30)`

## VWAP Deviation Baseline

Using the bar-level `vwap` column (Alpaca's within-minute VWAP):
- Compute trailing 15-min dollar-weighted average price = sum(vwap × volume, 15) / sum(volume, 15).
- `vwap_dev_15 = (close - trail_vwap_15) / trail_vwap_15`
- Similarly `vwap_dev_30` for 30-min window.

Primary vwap_dev variable: `vwap_dev_15` for H=15, `vwap_dev_30` for H=30.

## Forward Return Target

Using bar-level close prices:
- `fwd_ret_15 = log(close_{t+15} / close_t)`
- `fwd_ret_30 = log(close_{t+30} / close_t)`

Forward returns are labeled at the minute grid; entry conceptually at T's close. This is an IC study.

## IC Computation

Cross-sectional rank-IC per minute-cross-section:
- For each minute timestamp with ≥ 20 symbols: Pearson correlation of cross-sectional ranks of
  signal vs ranks of forward return.
- Report mean IC and day-clustered t-stat (cluster = trading day, ~400 cross-sections per day).
- Day-clustered t: t = mean_IC / (std_of_daily_mean_IC / sqrt(n_days)).

## Orthogonalization

Per-minute cross-sectional residualization:
- For each minute, OLS regress fwd_ret_H on vwap_dev_H (using rank or raw values, cross-sectionally).
- Take the residual as `fwd_ret_H_resid`.
- Compute rank-IC of ofi_15 / ofi_15_norm / ofi_30_norm against fwd_ret_H_resid.

## Canary Test

10 seeds: for each seed, shuffle fwd_ret_H within each minute cross-section, recompute rank-IC
across all cross-sections, report mean IC. The null band = [2.5th, 97.5th percentile] of these
10 shuffle-mean ICs (approximate; ideally 100 seeds but 10 suffices as a sanity check).

## Cost Gate

Measure median bid-ask spread across the liquid symbol set (from quotes: median of (ask_price -
bid_price) / mid_price per quote update). One-way cost estimate = 0.5 × spread.

Decile L-S gross bps: top-decile minus bottom-decile of the best OFI signal, mean forward return
× 10000 (bps). Compare to 2 × one-way cost (round-trip). Report whether gross > round-trip cost.
