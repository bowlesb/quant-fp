# W11 CERTIFY — method (18-month deep re-run)

**Registered intent:** re-run the EXACT W11 overnight-beta construction on the new 378d (18-month) bar
history — the decisive test of whether the +75 bps/day overnight L/S found on 126d (n=3 rebalances) is a
**durable beta-risk premium** or the **Dec2025–Jun2026 crypto/quantum/AI open-gapper regime confound**.

Read/data only. Wrote ONLY new `certify_*` files; did NOT overwrite the original 126d `results.json` /
`verdict.md`. Production code untouched; computed via `ops/sandbox.sh` (read-only `/store`).

## Data
- `certify_daily.parquet` built by `certify_build_daily.py` — identical RTH aggregation to the original
  `build_daily.py` (RTH = UTC minute [810,959] = 09:30–16:00 ET; `rth_open` = first RTH bar open = 09:30
  print; `rth_close` = last RTH bar close; `dollar_vol` = Σ close·volume; require ≥30 RTH bars/day).
- **Depth discipline (no mixing):** only symbols with **≥378 date partitions** are included. 4420 deep dirs
  scanned → **3838 symbols × 378 dates, 2024-12-11 .. 2026-06-16** (the full 18-month window).
- Per (symbol,date): `overnight = rth_open / prev_close − 1`, `intraday = rth_close / rth_open − 1`,
  `ret24 = rth_close / prev_close − 1`. ts is genuine UTC (13:30 UTC = 09:30 ET). prev_close is the
  per-symbol shifted RTH close.

## Construction (reused exactly from W11)
- **Market = SPY** (spec: rolling 60d OLS on SPY daily return). SPY's own 24h `ret24` is the regressor.
- **Beta per name** = trailing 60-trading-day OLS of name `ret24` on SPY `ret24`, point-in-time (window
  EXCLUDES the estimation date), re-estimated every **21 trading days** (monthly).
- **Universe = top-N liquid SINGLE stocks** by median daily dollar-volume, requiring ≥350 days. ETFs / index
  products (SPY, QQQ, all XL* sector ETFs, leveraged/inverse products, crypto-ETFs) are EXCLUDED from the
  tradeable beta sort — a sector ETF has beta≈1 by construction and SPY itself is the market (beta≡1), so
  including them pollutes a single-stock beta sort. **Primary N_LIQUID = 200 → 40/quintile**; sensitivity
  run at 150 and 300.
- **Each rebalance:** sort the universe into 5 beta QUINTILES; form high-minus-low-beta L/S (equal weight
  within leg). Realize over the next 21-day holding period (strictly forward of the beta window) THREE ways:
  OVERNIGHT-only, INTRADAY-only, 24h. Per-rebalance observation = mean daily L/S over the hold (the
  NON-overlapping bootstrap unit). **15 non-overlapping rebalances** (vs 3 on 126d → 5× the power).

## Certification controls added (the whole point)
1. **The confound control.** Re-run EXCLUDING the crypto/quantum/AI-speculation open-gapper cohort
   (`SPECULATION_COHORT`: MSTR/MARA/RIOT/COIN/CLSK/HOOD/IONQ/QBTS/RGTI/BBAI/SOUN/ASTS/RKLB/SMR/OKLO/CEG/
   GEV/AFRM/UPST/PLTR/APP/SMCI/… — 14 of these were actually in the top-200 universe and were removed). If
   the overnight>intraday split VANISHES without the gappers, W11 was the regime confound → KILL.
2. **Sub-period stability.** Split the 18mo into 2025-H1 / 2025-H2 / 2026-H1 — is the split STABLE across
   regimes, or concentrated only in the speculation window?
3. **Auction-slippage stress.** Beyond the 3 bps/side quote-spread proxy, add **5 bps/side MOO/MOC auction
   slippage** (the overnight bet enters at the close auction and exits at the open auction). Net-of-cost is
   reported both spread-only and spread+auction. Turnover-aware: spread charged on the changed leg fraction.
4. **Walk-forward OOS** = second half of the 15 rebalances. **Shuffle-canary** = permute beta→name mapping.
5. **Per-rebalance bootstrap** (10k resamples, 95% CI) on the non-overlapping per-rebalance overnight L/S.

Scripts: `certify_build_daily.py`, `certify_analyze.py`. Output: `certify_results.json`.
