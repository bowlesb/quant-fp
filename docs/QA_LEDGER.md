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

## Automated invariant suite (2026-06-12) — invariants are now CHECKS, not prose

The standing invariants above are now an automated, fail-loud suite. **This is the durable
answer to the lesson that defines this role:** vigilance no longer depends on any agent
noticing — a violation fails CI with a clear message.

- **`scripts/qa_invariants.py`** — CI-able runner. `python3 scripts/qa_invariants.py`
  (exit 1 on any FAIL), `--list`, `--only a,b`. DB access via the documented
  `docker compose exec psql`; override with env `QA_PSQL` for in-network/CI runs.
- **`tests/test_invariants.py`** — the same checks as pytest (one source of truth); SKIPs when
  the DB is unreachable so the clean `make test` container stays green.
- **`tests/test_session_date.py`** — near-midnight-UTC ET-session-date regression (task #13).
- **`tests/fixtures/known_funds.txt`** — frozen 5,284-name fund denylist (independent gate input).

| invariant | maps to | gates |
|-----------|---------|-------|
| `universe_is_equities_only` | composition | latest universe has 0 is_etf_like members. **NECESSARY-NOT-SUFFICIENT** — shares the builder's regex, so tautologically green on a fresh build; guards only against the builder dropping the classifier. |
| `universe_no_known_funds` | composition | **INDEPENDENT gate** — 0 members on the frozen denylist (catches re-introduction even if the regex is weakened) + warns on a separately-maintained leverage/inverse token heuristic. |
| `universe_sessions_valid` | I1/calendar | no weekend member trade_dates; latest member date doesn't lead the latest ingested ET session (UTC-vs-ET session-date bug, task #13). |
| `calendar_et_correct` | I1 | ACTIVE set: minute_of_day/day_of_week == ET wall-clock; all RTH. Legacy sets reported, not gated. |
| `bars_integrity` | values | OHLC/minute-grid sanity (extended-hours bars are expected, reported not failed). |
| `backfill_realtime_parity` | I2 | stream vs backfill close agree within 0.2% on overlap (≤1% mismatch). |
| `trade_agg_parity` | I2b | trade-agg stream vs backfill ≥98% within 2% on overlap (SKIPs if no overlap). |
| `pit_universe_membership` | I3 | ACTIVE set: every (symbol,date) feature row is an in-universe member that date. |
| `warmup_coverage` | I4 | ACTIVE set: no ragged-warmup / dead feature. Legacy sets reported, not gated. |
| `no_inf_no_degenerate` | I5 | no Inf; predictions not score-degenerate; per-ts labels demeaned (avg, not max). |

**Set scoping:** calendar/warmup/pit gate the ACTIVE set (`QA_ACTIVE_SET` or the highest
set_version present; currently **v1.1.1**). Legacy sets (v1.0.0, the frozen-dirty v1.1.0
fixture) are reported informationally — target one with `QA_ACTIVE_SET=v1.1.0` to reproduce its
historical FAIL (the suite's own regression fixture).

**First full-run evidence (2026-06-12, pre-panel-rebuild-completion):** the
`universe_is_equities_only` check FAILED on the dirty 209-fund universe and now PASSES (0/1000)
on the clean rebuild — the before/after proof. The suite also localized the legacy dirt:
UTC-calendar leakage is confined to v1.0.0 (32,220 rows); v1.1.0 and v1.1.1 are ET-clean. The
117k-row PIT leak is 96% (209/217) the ETF symbols — it resolves when the clean panel + history
land. `backfill_realtime_parity` read borderline 1.14% mismatch (just over the 1% gate) — see
below.

## Open concerns — severity-ranked (update status each wake)

| sev | id | concern | status |
|-----|----|---------|--------|
| P1 | parity-1.14 | `backfill_realtime_parity` = 1.14% (7,731/678k bars >0.2% close diff). **DRILLED (task #14, scripts/parity_drill.sql), two drivers:** (1) **KLAC stream close is a persistent EXACTLY-10× the backfill close** (2312.47 vs 231.01) across BOTH settled days — a standing feed scaling/decimal BUG, not a split-date artifact; all 833 KLAC bars = the entire >10% band (~11% of mismatches). (2) **~87% are small (<1%) systematic per-symbol offsets** — ~15-20 symbols (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS…) mismatch 100% of their bars by <1%, i.e. a methodological close-price difference (official/consolidated close vs last-trade minute close), not random tick loss. Even after fixing KLAC the residual is ~1.02%. | OPEN — driver breakdown committed; FIX owned by prod-architect (KLAC 10× + canonical-close decision). Gate stays 1% (catching real issues). |
| P1 | tradeagg-close-hour | **Settled-day trade-agg parity (task #15, scripts/trade_agg_parity_settled.sql), 2026-06-11, 50 syms/6,058 min:** core RTH parity is GOOD — n_trades within-2% 98.05% (corr 0.9997), **tick-rule SIGN agreement 99.82%** (the hardest threat, solid). BUT two caveats before M2 scale: (a) **16:00-ET close-hour n_trades-within-2% collapses to 14%** (closing-cross/late prints diverge live-vs-REST; minute-boundary state init suspect) → OFI features at/after the close untrustworthy; (b) **coverage mismatch** — backfill 34,784 min vs stream 18,860 (stream RTH-concentrated), 12,802 stream-only minutes unexplained. | OPEN — at-scale data path owned by prod-architect; QA re-runs as scale grows. Gate OFI on excluding closing minutes + explaining coverage. |
| P2 | denylist-regex-dependence | The frozen known-fund denylist (5,284) and `is_etf_like` are both NAME-regex derived, so a fund whose name evades the regex slips BOTH the builder and the denylist. True independence needs an external issuer/share-class metadata source (Alpaca classes stock & ETF alike as us_equity — no broker-side split). | OPEN — tracked upgrade: wire an external fund/share-class signal; until then the denylist is necessary-not-sufficient |
| P0 | etf-contamination | ~207 of 1000 universe members (~21%) are ETFs/ETNs/leveraged-inverse/VIX-futures funds (SOXL, TQQQ, SQQQ, TNA, UVXY, VXX, UPRO, SPXU, TSLL...), NOT single-name equities. They reached the feature panel (1.59M feature_vector rows / 207 symbols) and were RANKED cross-sectionally against stocks. The price-only "no edge" verdict was drawn on this contaminated cross-section -> NOT trustworthy until re-run clean. **ROOT CAUSE:** the ETF filter `is_etf_like` EXISTS and is wired into the executor (main.py:112) but is NEVER called in `quantlib.universe.select_universe` — the function that builds universe_membership. Filter applied at order layer, skipped at research/universe layer (asymmetry hid it: live trading clean, offline panel polluted). **REAL FIX:** thread asset name into SymbolStats + filter `is_etf_like` in select_universe (clean on every rebuild) — not the one-off SQL exclusion (band-aid). Classifier + clean scaling list staged in scripts/etf_exclusion.sql. | PARTIAL — select_universe fixed (814e548); universe rebuilt clean & now **AUTOMATED-GREEN on the live universe** (`universe_is_equities_only` + independent `universe_no_known_funds`, 0/1000 at 2026-06-12, was 209). Remaining: clean panel (v1.1.1) + full history (tasks #2/#12), then RE-RUN the battery (task #4). |
| P1 | UTC-calendar-legacy | UTC-calendar leakage now AUTOMATED (`calendar_et_correct`) and LOCALIZED: 30,970 v1.0.0-historical + 1,250 v1.0.0-stream rows; **active v1.1.0 and v1.1.1 are 0/ET-clean.** The serving path is clean; the dirt is confined to the deprecated v1.0.0 set. | OPEN-LOW — purge v1.0.0 to clear the legacy red; active sets gated green |
| P1 | warmup-unmonitored | now AUTOMATED (`warmup_coverage`): ragged-warmup + dead-feature detector, gated on the active set. Confirms v1.0.0's 5 micro features are 99.5% NaN (dead); active set carries none. | MITIGATED — monitored fail-loud; build-time warmup assert still desirable (defense in depth) |
| P1 | preds-degenerate | predictions ~80% within 1bp of 0 → basket was tie-break noise | MITIGATED — executor degeneracy guard added; preds still non-tradeable |
| P1 | pit-leak | now AUTOMATED (`pit_universe_membership`): the leak is far bigger than the earlier ~14 estimate — **117,047 (symbol,date) feature rows in v1.1.0 are not in-universe members for that date, across 217 symbols of which 209 (96%) are the ETFs.** The panel was built over a current symbol set across all history, not strict PIT membership. Resolves when the clean v1.1.1 panel + history land (tasks #2/#12). | OPEN — gated on active set; will go green when v1.1.1 history is PIT-correct |
| P2 | view-fanout | training_data 2× horizon fan-out | LOW — trainer filters horizon; harden the view |

## Resolved (kept for history)

- Compression: 0/74 → 68/74 chunks (DB 6.8GB→2.7GB).
- day_of_week ET-correct across all 662k historical rows; per-ts demean exact; no Inf.
- Micro features 99.9% NaN universe-wide → dropped from the v1.1.0 set (identity-leak risk).
