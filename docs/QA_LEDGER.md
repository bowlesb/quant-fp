# QA Ledger — standing data-integrity registry

Owned by the Data QA Tester. Read + updated EVERY wake. Repetition of the top pressing
concerns is the POINT — re-rank all open items by severity and always surface the worst,
even if reported before. Forward-looking: anticipate what breaks given where we're going.

## Standing invariants (re-check every wake with live queries)

- **I1 — Calendar/time:** minute_of_day/day_of_week equal true ET time for ALL sources
  (historical/stream/live), no off-grid ts, DST-correct (America/New_York), no UTC leakage.
- **I2 — Parity:** backfill vs real-time aggregates + feature vectors identical on overlap
  (replay-equivalence); the settled-day bar-parity gate is met before trusting IC.
- **I2b — TRADE/QUOTE parity (the hard, weakest case — own it explicitly):** trade-based
  features (trade_imbalance/large_print_cnt/trade_intensity) and quote-based (spread_bps/
  quote_imbalance) come from OUR aggregation of a lossy live feed vs the complete REST
  record. Bars ~99.4%; trade-aggs only ~95% within 2% on a tiny sample, NEVER validated on
  a settled day at scale. Threats to verify before trusting any trade feature: (1) dropped
  live ticks vs complete REST; (2) tick-rule sign depends on order+last_price state —
  live out-of-order/late delivery diverges; (3) trade CONDITION filtering (odd-lot/out-of-
  seq/late) must match live↔backfill — confirm we filter identically (or at all); (4)
  minute-boundary state init. This is the parity that matters MOST (order flow ≈ the real
  edge candidate) and is currently the LEAST proven. Read 2026-06-11: 98.2% within 2% same-day (de-risk). Gate: settled-day trade-agg parity at
  scale before any trade feature enters a trusted model. Blocked-by: universe-wide trade/
  quote ingestion (the Architect's sharded-ingestion decision).
- **I3 — PIT universe:** feature rows exist ONLY for that date's universe members; per-ts
  label cross-section demeaned (median ~0); no derived/leveraged tickers leaking in.
- **I4 — Coverage/warmup per feature (the one we missed):** NO feature silently
  NaN-degraded. Usable panel = [start + max_feature_lookback, end − label_horizon]. Each
  feature's required lookback must be served by backfill that PREDATES the panel window.
  Monitor NaN-rate per feature per date; a new long-lookback feature must not NaN the
  early panel. Build-time should ASSERT warmup adequacy, not silently emit NaN.
- **I5 — Values/tradeability:** no Inf; bounded outliers (vol_z fat tail noted);
  predictions not score-degenerate (distinct scores, no tie-break-decided basket).

## Open concerns — severity-ranked (update status each wake)

| sev | id | concern | status |
|-----|----|---------|--------|
| P0 | UTC-today | today's (2026-06-10) historical panel has ~4477 UTC-calendar rows reaching training_data (insert-not-replace) | OPEN — fix = rebuild DELETE-then-insert + purge |
| P0 | UTC-stream | feature-computer wrote UTC-calendar `stream` rows (stale pre-DST code) | OPEN — purge + add serving-path ET assertion |
| P1 | warmup-unmonitored | per-feature warmup/coverage was UNMONITORED and the build has no warmup guard (momentum worked only because the panel starts 22 trading days after bars start — luck, not enforced) | OPEN — add NaN-by-feature-by-date probe + build-time warmup assert |
| P1 | preds-degenerate | predictions ~80% within 1bp of 0 → basket was tie-break noise | MITIGATED — executor degeneracy guard added; preds still non-tradeable |
| P2 | pit-leak | ~14 non-member (derived/leveraged) rows leak into training_data | OPEN — hard-filter members + exclude derived tickers |
| P2 | view-fanout | training_data 2× horizon fan-out | LOW — trainer filters horizon; harden the view |

## Resolved (kept for history)

- Compression: 0/74 → 68/74 chunks (DB 6.8GB→2.7GB).
- day_of_week ET-correct across all 662k historical rows; per-ts demean exact; no Inf.
- Micro features 99.9% NaN universe-wide → dropped from the v1.1.0 set (identity-leak risk).
