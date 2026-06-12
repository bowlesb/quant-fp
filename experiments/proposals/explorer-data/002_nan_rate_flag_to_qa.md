# Proposal 002 — FLAG-FOR-QA: intraday-return features are 12-20% NaN on v1.1.1, not "0.000%"

**Author:** explorer-data | **Date:** 2026-06-12 | **Type:** QA-flag (not an experiment queue item) | **Status:** FILED (sent to qa, cc modeller)
**Lens:** data archaeology — a panel oddity that nuances EVERY verdict computed on v1.1.1.

## Claim contradicted
QA_LEDGER / ROADMAP M1 state the clean v1.1.1 panel is "NaN 0.000% on all 21 features" (5,525,040 rows / 0.000% NaN). That is FALSE for the intraday-return and volatility features.

## Evidence (full 5,525,040-row v1.1.1 panel; metric = count(vector[i]='NaN'::float8))

| feat idx | name | pct_nan (all rows) | pct_nan (excl 9:30 open) |
|---:|---|---:|---:|
| 1 | ret_5m | 13.44 | 5.76 |
| 2 | ret_15m | 13.52 | 5.84 |
| 3 | ret_30m | 12.38 | 4.61 |
| 4 | ret_60m | **20.06** | **12.96** |
| 5 | vol_30m | 16.87 | 9.50 |
| 6 | vol_60m | 16.87 | 9.50 |
| 7 | vol_z_30 | 16.87 | 9.50 |
| 8 | vwap_dev | 0.00 | 0.00 |
| 9 | range_pct | 0.00 | 0.00 |
| 10 | gap_from_open | 0.00 | 0.00 |
| 11 | rel_ret_30m | 12.38 | 4.61 |
| 12-13 | calendar | 0.00 | 0.00 |
| 14-19 | mom_1d..mom_5d_rel | 0.00 | 0.00 |
| 20-21 | mom_10d, mom_10d_rel | 0.01 | 0.01 |

## Two mechanisms (both real, distinct)
1. **9:30 ET open (minute_of_day=570): 100% NaN for every intraday-RETURN feature.** No 5/15/30/60-min lookback exists at the open. 450,208 rows = the first cadence of every trading day. Correct-by-construction, but the open cross-section is ranked on ONLY {vwap_dev, range_pct, gap_from_open, calendar, momentum} — a different, smaller feature subset than every other cadence.
2. **Mid-session 5-13% NaN = missing N-min-lagged bars in THIN names.** Not warmup. Concentrated in high-nominal-price, thin-trade S&P members: NVR 60.7%, LFUS 50.6%, GWW 48.0%, CW 47.0%, TPL 45.6%, TDY 45.1%, MUSA 44.9%, AEIS 43.4%, MTD/NVMI 41.8%. ~173 names >10% mid-session NaN; 611 names <10%. ret is NaN whenever the lagged minute had zero trades.

## Why it matters (consumption path)
`quantlib.research.load_panel` (line 48) maps `None -> math.nan` straight into X. LightGBM handles NaN natively (learned default split direction) — so these rows are **neither dropped nor imputed**. The M1 price-only "no edge" verdict, and every battery IC, were computed INCLUDING ~13% NaN-ret_5m rows and the all-NaN-return open cross-section. The verdict may well be robust to this (it's a "no edge" either way), but:
- It means warmup_coverage invariant I4 ("no feature silently NaN-degraded") is either not scanning the in-vector 'NaN'::float8 sentinel, or its "0.000%" refers to NULL/0.0-stored values rather than the NaN sentinel. A 20% NaN on ret_60m is exactly the silent degrade I4 exists to fail-loud on.

## Asks (to QA, their lane)
1. Reconcile: does `warmup_coverage` scan `vector[i]='NaN'::float8`, or only NULL/0.0? If the former, ret_60m should be RED.
2. Decide: should the 9:30-open cross-section (all return features NaN) be in the trained/traded panel? It's the first cadence of every day on a degraded subset.
3. Update the ledger/ROADMAP "0.000% NaN" line to the true per-feature rates, or clarify what it measures.

## Falsifier / what would make this a non-issue
If QA confirms the harness's NaN handling is intentional AND the verdict is shown insensitive to it (e.g. re-run the battery excluding NaN-ret_5m rows + the open cadence → same "no edge"), then this is documented-and-fine, not a defect. That sensitivity re-run is the clean close.

## Disposition (Lead / QA fill in)
_pending — sent to qa 2026-06-12, cc modeller_
