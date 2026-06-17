# W12 — Net share issuance / buyback L/S (LIQUID) — Method

Pre-registered hypothesis: `hypothesis.md` (lens L5 fundamentals; survey #2, Pontiff–Woodgate 2008 /
Fama–French 2008 — net-share-issuance is one of the few anomalies documented to SURVIVE in big stocks).
Read/data only; no production code touched; compute via `ops/sandbox.sh` (fp-dev, store mounted RO).

## Universe (LIQUID)
- `build_universe.py`: top 500 names by **median daily dollar-volume** over the full 378-trading-day bar
  history (`/store/raw/bars`, regular session 13:30–20:00 UTC, dollar-vol = Σ close·volume per session day,
  ≥200 session days required). Min median dollar-volume in the top-500 ≈ $139M/day.
- `map_and_fetch.py` step 1: map the 500 symbols → CIK via SEC `company_tickers.json` (authoritative;
  dot/dash/strip variants). **489/500 matched**; the 11 unmatched are sector-SPDR ETFs (XL*) which have no
  CIK/shares and correctly drop out.

## Shares-outstanding (point-in-time)
- `map_and_fetch.py` step 2: one GET per CIK to `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`
  (User-Agent `quant-fp research ben.bowles@gmail.com`, 0.12s sleep, cached to `cache/`). Extract a long
  table `(symbol, tag, end, filed, val)` from preferred tags:
  `dei:EntityCommonStockSharesOutstanding` →
  `us-gaap:CommonStockSharesOutstanding` → `CommonStockSharesIssued` →
  `WeightedAverageNumberOfSharesOutstandingBasic` → `…Diluted`.
- **PIT / look-ahead-safe**: a value is only usable once `filed` ≤ the as-of date. Restatements with a
  future `filed` are never used.
- ~85% of usable per-rebalance signals come from the clean instant `dei:EntityCommonStockSharesOutstanding`
  tag; weighted-average tags are a fallback only.

## Net issuance
- For each (symbol, rebalance date): take PIT-current shares (filed ≤ t) and PIT-prior shares
  (filed ≤ t−365d), **using ONE consistent tag** (first in priority order that yields both endpoints) so the
  ratio is not corrupted by mixing measures. Same-period multi-member rows are reduced by median.
- Require the two period-ends to be 270–460 days apart (a genuine ~1y change).
- **Split-adjust** the OLD count to the NEW basis using `corporate_actions_pit` splits between the two
  period-ends (multiply old shares by Π split_ratio for ex-dates in (end_prior, end_now]).
- `net_issuance = log(shares_now / shares_prior_adj)`. Drop `|issuance| > 1.5` (>~4× growth or >75% shrink in
  1y = a residual split/data artifact, not a real buyback/issue).

## Portfolio test
- Rebalance schedule: every `HOLD_DAYS = 63` trading days (~3 months), **non-overlapping** holds → **5
  rebalances** on the 378-day window. Honest: cross-sectional breadth (~460 names/rebalance) is the power,
  NOT the 5-point time series.
- Each rebalance: rank liquid names with finite issuance; **equal-weight quintile L/S** — long the bottom 20%
  (buyback/shrinking-share), short the top 20% (issuing). Forward return per name = close_{t+63}/close_t − 1
  from the daily close panel (`build_daily_panel.py`). Portfolio gross = mean(long) − mean(short).
- Cost: liquid one-way spread ≈ 2.5 bps → ~5 bps round-trip, charged on BOTH legs each rebalance
  (conservative full-turnover assumption); a 2× cost stress is also reported.

## Gates
- **Shuffle-canary**: permute issuance vs forward return within each rebalance (20 seeds) → should be ~0.
- **Per-symbol demean**: subtract each symbol's mean forward return, redo the L/S.
- **Walk-forward OOS**: split the rebalance series in half; the long-buyback direction is fixed a-priori
  (no in-sample sign fitting), so OOS = the later half.
- **Per-rebalance bootstrap** (10k) on the net-of-cost series; DECISIVE if the OOS net-of-cost CI > 0.

Files: `build_universe.py`, `map_and_fetch.py`, `build_splits.py`, `build_daily_panel.py`, `run_w12.py`
(+ `diagnose.py`). Raw numbers in `data/raw_results.json`.
