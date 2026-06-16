# HF01 — method (exact operational definitions)

## Universe + data
- Symbols: the deep-quote megacap set with ≥21 quote-days — MSFT, AVGO, AMD, TSLA, AAPL, NVDA, SPY, AMZN,
  META, GOOGL, QQQ, NFLX. Use all completed quote-days available per name (exclude the empty current day).
- Source: `/store/raw/quotes/symbol=<S>/date=<D>/data.parquet` (bid_price, bid_size, ask_price, ask_size, ts
  UTC) and `/store/raw/trades/...` (price, size, ts UTC). ALL ts are GENUINE UTC — 13:30 UTC = 09:30 ET
  (RESEARCH_PITFALLS #1; convert with `dt.convert_time_zone("America/New_York")` if you need ET, never read
  `.hour()` off a UTC ts). RTH only: keep quotes/trades with UTC minute in [810, 1190) (09:30–15:50 ET);
  drop the first 5 min (warmup) and ≥15:50 (MOC distortion).

## Time grid + the target (MID return, never trade-price)
- Resample to a clean sub-minute grid by SECONDS bucket (e.g. 10s bars) within RTH: for each (symbol, 10s
  bucket) compute the prevailing top-of-book bid/ask (last quote in the bucket) → mid = (bid+ask)/2.
- Forward target: `fwd_mid_ret_h = mid[t + h] / mid[t] − 1` for h ∈ {1, 2, 5} minutes (i.e. +6, +12, +30
  buckets at 10s). MID-to-MID — this removes bid-ask bounce mechanically.
- Signal lag: every signal at decision time t uses only quotes/trades with ts < t (strictly trailing); lag by
  one bucket to be safe.

## Signals (trailing window w ∈ {30s, 60s, 120s})
- `qimb_w` = mean over the trailing w of (bid_size − ask_size)/(bid_size + ask_size), per quote, time-weighted
  or simple-mean (state which).
- `ofi_w` = CKS order-flow imbalance over trailing w: sum of signed top-of-book changes
  (bid side: +Δbid_size if bid_price up/equal-and-size-up, −prev_bid_size if bid_price down; mirror on ask),
  per the H2 definition but over the short window.
- `stflow_w` = tick-rule signed trade volume over trailing w / total trade volume over w (∈ [−1,1]).

## Scoring (the gates, in order)
1. **Spearman rank-IC of each signal vs `fwd_mid_ret_h`**, per (symbol) or pooled within time-blocks; report
   per (signal × w × h). Day-clustered t (each day one cluster).
2. **Shuffle canary FIRST** — 10 seeds, permute `fwd_mid_ret_h` within each day (or each 30-min block); the
   signal IC must lie OUTSIDE the 2.5/97.5 canary band. Inside → KILL that cell.
3. **Per-symbol demean** — subtract each symbol's mean `fwd_mid_ret_h` (within-split for OOS) before IC.
4. **Walk-forward OOS** — split days TRAIN/OOS ~50/50 (by date), demean within split, report OOS IC + t. A
   window/horizon chosen after seeing TRAIN must replicate OOS (hold-out rule).
5. **Turnover-compounded cost gate (decisive):** form a long/short or signal-thresholded book that rebalances
   every h minutes; entry crosses the spread (buy@ask, sell@bid); book the realized MID-to-MID P&L MINUS the
   full measured round-trip spread (per-name, ~0.5–3 bps × 2) on every rebalance. TURNOVER = fraction of book
   re-traded per period. Report gross, turnover, and NET = gross − turnover×round_trip_cost at the measured
   spread AND at 2× (stress). Sweep a no-trade band / persistence threshold (only act when |signal| or its
   sign-persistence exceeds a bar) to cut turnover; report the band that maximizes net. KILL if no cell nets
   positive after this.

## Outputs
- `results.md`: per (signal × w × h) IC + day-clustered t + canary pass/fail; demean version; OOS version;
  the turnover/net table at measured + 2× cost with the no-trade-band sweep; per-name measured spread used.
- `verdict.md`: KEEP-AS-LEAD / AMBIGUOUS / KILL per the pre-reg gates. The decisive number is the OOS
  turnover-compounded NET at the measured spread. Honest power note (some names only ~21 quote-days).

## Compute
- Sandbox only: `MEM=12g CPUS=8 ops/sandbox.sh "python <script>"`. polars not pyarrow. Vectorize. The 10s
  resampling of ~2,400 quotes/min × 12 names × ~21–63 days is sizeable — scope to the deepest few names first
  if memory-bound, and report power honestly.
