# Method — H1 proof-of-loop probe (single live RTH session, CPU-only, read-only)

**Date run:** 2026-06-15 · **Mode:** read-only via `docker exec feature-computer`. No code/feature edits.
All throwaway scripts in `/tmp` (`/tmp/probe_h1b.py`).

## Data paths (live `/store` parquet, per-minute, 8 shards)
- Signal: group `price_volume`, column **`vwap_deviation_30m`**
  `/store/group=price_volume/v=*/source=stream/date=2026-06-15/data-<shard>-<epoch>.parquet`
- Forward return source: group `price_returns`, columns `ret_5m`, `ret_15m`
  `/store/group=price_returns/v=*/source=stream/date=2026-06-15/...`
- Liquidity proxy: group `volume`, column **`dollar_volume_1m`** (trailing, at minute t)
  `/store/group=volume/v=*/source=stream/date=2026-06-15/...`

Each file has columns `symbol`, `minute`, features. The filename `<epoch>` equals the bar `minute`
(verified: file at epoch for 14:00 UTC has `minute == 2026-06-15 14:00:00 UTC`). There are 8 shard files
per minute; a minute's full cross-section is the concat of all 8 shards.

## Universe / RTH window
- RTH = 13:30–20:00 UTC (09:30–16:00 ET). 364 RTH minutes available.
- **Observed universe is ~2,800–2,900 unique symbols per RTH minute** (NOT the ~500–630 the hypothesis
  assumed — the live store is much wider). This *increases* cross-sectional power.
- **Dedup:** SPY/QQQ/IQM-type index ETFs (SPY, QQQ, IWM) are replicated identically across all 8 shards.
  An un-deduped join produced a small cartesian blowup (median ~4,400 rows) and a spurious high-variance
  HIGH-LIQ IC. Fixed with `.unique(subset=["symbol"])` per group before joining. All reported numbers are
  deduped. (The pre-dedup run is noted in results.md as a caveat.)

## Sampling (to bound cost)
Sample **every 5th RTH minute** → 73 sampled minutes; ~60–68 yield a usable forward cross-section
(the last H minutes near 20:00 UTC have no t+H forward file).

## Forward-return construction (look-ahead care)
The store's `ret_Hm` is a **trailing** return at the stamping minute (return over the prior H minutes).
To get a **point-in-time forward** return for an entry at minute t, I read `ret_Hm` from the file stamped
at **t + H minutes** and join it back onto the t cross-section by `symbol`:

    fwd_ret(symbol, t) = ret_Hm(symbol, t+H)   # = realized return from t to t+H

- No future information enters the **signal** side: `vwap_deviation_30m` and `dollar_volume_1m` are both
  read at t only.
- The forward return is a strictly-after-t realized quantity, joined by symbol — standard forward-IC.
- Caveat documented in verdict: this is a within-minute cross-sectional IC, **not** a tradeable backtest
  (no entry-price tradeability gate, no cost, no turnover). Entry-price/≥09:35 tradeability is irrelevant
  here because we never claim an executed return — only the sign/concentration of the cross-sectional rank
  relationship. Horizons tested: **H=5 and H=15**.

## Statistic
Within each sampled minute, **Spearman rank-IC** between `vwap_deviation_30m`(t) and `fwd_ret`(t).
Pool across minutes: report mean IC, std, and rough t-stat = mean / (std / sqrt(n_minutes)).

## Liquidity split
Within each minute, split the cross-section at the **median `dollar_volume_1m`**: HIGH-LIQ = at/above
median, LOW-LIQ = below. Compute the rank-IC separately in each half. Report both magnitudes and the
illiquid/liquid |IC| ratio. Falsifier threshold (pre-registered): illiquid |IC| > 2× liquid |IC| ⇒
evidence AGAINST H1.

## Leakage canary
Within each minute, shuffle `fwd_ret` across symbols and recompute the pooled IC; it must collapse to ~0.
Ran single-seed (in the main script) AND a 10-seed stability check for H=15.
