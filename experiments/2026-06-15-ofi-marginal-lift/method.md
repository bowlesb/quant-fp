# Method — H2: OFI marginal lift over vwap_dev (2026-06-15, CPU-only research cycle)

All data built by this explorer from Alpaca historical SIP, OUT of the production store.
Scratch data lives in `experiments/data/` (`bars.pkl`, `ofi_minute.pkl`) — never written to `/store`.

## Universe (what I ACTUALLY ran — honest, bounded)
- Started from a fixed liquid US large-cap set (S&P-large-cap style names), deduped, dot-tickers (e.g. BRK.B)
  dropped (Alpaca minute endpoint unreliable for them). **289 names had clean 1m SIP bars** → bars panel.
- **For the OFI/trades panel the cost was the binding constraint.** SIP trades are huge (mega-caps 200k–800k
  trades/day; ~5–9s per symbol-day fetch with pagination). To finish a bounded CPU cycle I:
  1. ranked the 289 names by total dollar-volume,
  2. EXCLUDED the ~20 heaviest mega-caps (AAPL/NVDA/TSLA/… — 20–30s/fetch each, infeasible),
  3. took the next very-liquid band: **ranks 20–100 = 80 names** for the trades/OFI build.
- **The matched OFI×bars test panel = 80 symbols.** Honest tradeoffs: this is a "liquid large-cap EXCLUDING the
  megacap top" cross-section, and it is survivorship-tilted (names alive/liquid TODAY). It is a marginal-IC
  orthogonality probe ONLY — any positive read must be re-run at full universe before an edge claim.
- (Engineering note for the MA: an earlier run accidentally launched multiple competing trade-fetch processes
  which tripped rate-limit backoff and stalled; a single clean vectorized-tick-rule process runs at
  ~5s/symbol-day. The 80×3 build took ~16 min. The container OOM-killed once on the full-panel analysis load
  and was restarted; live capture restart policy recovered it. Data files survived.)

## Days
- Bars fetched for 9 recent trading days (2026-06-02 … 06-12), RTH 13:30–20:00 UTC; today (06-15) EXCLUDED.
- **The trades/OFI build, and therefore the matched test, uses 3 days: 2026-06-09, 06-11, 06-12** (trimmed
  from the planned 8 purely for the time budget — stated honestly; this materially shrinks statistical power,
  see verdict).

## Bars (1m SIP)
- `StockBarsRequest`, `TimeFrame.Minute`, `DataFeed.SIP`, batched 100 symbols/request, per day.
- Deduped on the (symbol, timestamp) index before use.
- **Session cumulative VWAP:** within each (symbol, day), `cum_vwap = cumsum(bar_vwap * volume) / cumsum(volume)`
  (bar_vwap from Alpaca; typical price (H+L+C)/3 as fallback if vwap missing).
- **vwap_dev = close / cum_vwap − 1** — the pre-registered true carrier baseline.

## Forward return (TRADEABLE — no same-bar / no 09:30-print look-ahead)
- Computed within each (symbol, day) so entry and exit are always in the SAME session.
- **fwd = close(t+H) / close(t+1) − 1**, H = 15 (primary). Entry is the bar AFTER the signal minute t
  (t+1 close), exit is t+H close. Signal at minute t uses only information available at the close of t.
- Cross-sectionally **demeaned within each minute** (`fwd_dm = fwd − mean_over_symbols(fwd)`) before IC.

## Trades / OFI (SIP, built by hand)
- `StockTradesRequest`, `DataFeed.SIP`, fetched **per-symbol per-day**, aggregated to the minute IMMEDIATELY,
  raw trades discarded (memory-bounded).
- **Tick-rule sign:** price > prev → +1; price < prev → −1; price == prev → carry last sign.
- Per (symbol, minute): `signed_vol = Σ sign·size`, `tot_vol = Σ size`, `ntrades`.
- Deduped on (symbol, minute) before joining to avoid the cartesian artifact flagged last cycle.
- Features (rolling WITHIN each session, so no cross-day leakage):
  - `signed_vol_z` = z-score of signed_vol over a trailing 30m window (min 10 obs).
  - `ofi_5 / ofi_15 / ofi_30` = rolling sums of signed_vol over 5/15/30 min.
  - `ofi_{w}_norm` = ofi_w / rolling tot_vol_w (flow as a fraction of volume).
  - OFI score uses {signed_vol_z, ofi_15_norm, ofi_30_norm}.
- **MOC exclusion:** OFI features are NULLED for minutes ≥ 15:50 ET (ET = UTC − 4h in June) per the Modeller
  spec, to avoid market-on-close flow distortion. The fwd-return / vwap_dev rows are kept.

## Test (3-arm within-minute rank-IC)
For each RTH minute cross-section (pooled across minutes × days):
- **baseline** = vwap_dev (also report ret_5m).
- **+OFI** = within-minute-standardize vwap_dev and each OFI feature, then SUM (equal weight; OFI block is the
  mean of its standardized members). Simple, stated explicitly — no fitted weights.
- **OFI-only** = mean of the within-minute-standardized OFI features.
- IC = within-minute Spearman rank correlation of score vs `fwd_dm`; require ≥20 names and ≥3 unique scores
  per minute. Pooled mean IC; rough t = mean_IC / (std_IC / √n_minutes) over the per-minute IC series.

## Gates
- **Shuffle canary:** permute `fwd_dm` within each minute (10 seeds), re-measure IC. Real signal must sit
  outside the canary's ±2σ band.
- **Crude net-of-cost:** decile long/short book per minute on each score; turnover = symmetric-difference of the
  long & short sets vs the previous minute / book size; net = gross/min − turnover × 2bps × 2 legs. Honest
  ballpark, NOT the full measured-cost model.

## Panel actually tested (from the run)
- H=15: **89,819 (symbol,minute) rows · 80 symbols · 1,128 within-minute cross-sections · 3 days**.
- H=5:  92,219 rows · 80 symbols · 1,158 cross-sections · 3 days.
- Median names per minute ≈ 80 (full panel present most minutes) — adequate width for a within-minute rank-IC;
  the weakness is the small number of DAYS (3), not the cross-sectional width.
- Scratch data: `experiments/data/bars.pkl`, `experiments/data/ofi_minute.pkl`. Throwaway scripts in `/tmp`.
