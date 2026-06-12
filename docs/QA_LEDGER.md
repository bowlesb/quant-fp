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

**POST-REBUILD full-suite evidence (2026-06-12, v1.1.1 COMPLETE: 5,525,040 rows / 613 dates /
785 symbols / 0.000% NaN, all 3 label sets recomputed):** 9 PASS / 2 FAIL. The clean panel
passes EVERY integrity axis — calendar ET-correct (0 bad / 5.5M), PIT-correct, no ragged/dead
feature, no Inf, and the **label-demean sub-check re-activated and PASSES** (fwd_30m/60m
avg|per-ts median|=0.000000, overnight 0.000076 — the avg-based threshold clears the lone
0.011 single-ts outlier). The 2 FAILs are BOTH real & owned: `no_extreme_backfill_jump` flags
KLAC 6/01 (artifact, 13 real events silent → flips green after the #17 re-fetch) and
`backfill_realtime_parity` 1.14% (#14). i.e. the panel the edge verdict was computed on is now
verified trustworthy on every axis except the two flagged backfill issues (momentum-only /
parity-overlap, both being fixed).

## Open concerns — severity-ranked (update status each wake)

| sev | id | concern | status |
|-----|----|---------|--------|
| P1 | exec-recon-one-directional | **NEW (unprovoked probe, 2026-06-12 ~12:48 ET): the live execution loop silently runs an UNBALANCED basket and reconciliation reports it CLEAN.** `reconcile()` (services/executor/main.py:315-330) flags ONLY *unexpected* broker positions (a symbol at the broker we didn't submit). It is structurally blind to the INVERSE — orders we submitted that REJECTED, never filled, or partial-filled. Evidence: 6/12 intended a market-neutral 3L/3S basket (KEEL/SATS/UUUU long, FLY/W/AMPX short); broker holds only SATS+1/UUUU+13/W−2 → **2L+1S actually filled, KEEL+FLY+AMPX NEVER FILLED**, yet every `reconciliation_log` row all day reads `ok:true, unexpected:[]`. The book is net long-skewed vs market-neutral intent and NO monitor caught it. ROOT CAUSE of the misses: **fixed $0.01 marketable-limit cross** (every 6/12 order crosses NBBO by exactly 1¢ regardless of price/spread) — fine on W@78/SATS@126 (~1bp, filled), too thin on KEEL@5.74/FLY@37.26/AMPX@16.99 where 1¢ vs the spread/quote-drift left them non-marketable (FLY limit 37.26 was already INSIDE bid 37.27 at submit). SECOND defect: `orders_log.status` is terminal-blind — only ever 'intended'→'submitted', never filled/rejected/canceled written back (main.py:157,264), so the DB ledger alone can't tell whether a submit worked (all-time orders_log filled=0 despite real fills). Impact: today's P&L is noise so harmless NOW, but once a real-edge model is live this silently corrupts every basket's neutrality and the M4 paper track record — the ETF failure-mode (false confidence) reproduced in the EXECUTION lane. | OPEN — FIX owned by Exec/Risk: (1) symmetric reconcile = also flag submitted-but-unfilled / partial / orphaned-open orders, (2) price/spread-scaled marketable-limit (not fixed 1¢), (3) write terminal status back to orders_log. QA will encode a `fill_reconciliation` invariant (submitted set == filled+accounted set per day) once Exec lands the fix. |
| P1 | parity-1.14 | `backfill_realtime_parity` = 1.14% (7,731/678k bars >0.2% close diff). **DRILLED (task #14, scripts/parity_drill.sql), two drivers:** (1) **KLAC: a persistent EXACTLY-10× divergence** (stream 2312.47 vs backfill 231.01) across BOTH days; all 833 KLAC bars = the entire >10% band (~11% of mismatches). **⚠️ DIRECTION CORRECTED (prod deep-dive vs Alpaca live ~2429): the BACKFILL is 10×-DEFLATED, the STREAM was CORRECT.** Root cause: a split landing mid-backfill left pre-split months fetched under the OLD adjustment basis (mixed split-adjustment states). The drill's *detection* of the 10× gap was right; my *attribution* (stream bug) was backwards. (2) **~87% are small (<1%) systematic per-symbol offsets** — ~15-20 symbols (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS…) mismatch 100% of their bars by <1%, i.e. a methodological close-price difference (official/consolidated close vs last-trade minute close), not random tick loss. Even after fixing KLAC the residual is ~1.02%. | OPEN — driver breakdown committed; FIX owned by prod-architect (KLAC 10× + canonical-close decision). Gate stays 1% (catching real issues). |
| P1 | backfill-split-jump | **NEW class (task #17): mixed split-adjustment states in backfill** — a split landing mid-backfill leaves pre-split months on the old adjustment basis, deflating prices (KLAC = confirmed 10× artifact). A >3× day-jump sweep flagged **11 names**; KLAC confirmed artifact, the other 10 are likely real moves/reverse-splits (prod verifying each). This is a price-integrity bug that reaches the PANEL (momentum features), not just the parity overlap. | OPEN — `no_extreme_backfill_jump` invariant live. **Prod verified all 10 non-KLAC names as REAL events** (ABVX/ASTC/BMNR/FIG/INHD/QXO/RXT/STI/STRC/WOLF, match Alpaca live to the cent) → encoded as 13 surgical (symbol, date) exemptions in tests/fixtures/known_corporate_actions.txt; **KLAC stays flagged** (artifact, re-fetch queued #17 → invariant flips green after). Corporate-action FEED owned by prod under **task #18** (Alpaca corp-actions API) to replace the manual allowlist. COMPOSES with prod's complementary check (latest backfill close vs fresh Alpaca quote within ~5% = current-deflation signature, zero corp-action data): theirs catches CURRENT deflation cheaply, mine catches HISTORICAL mid-series steps at depth. |
| P1 | tradeagg-not-at-scale | **The "at-scale" trade-agg parity proof is NOT YET REAL (task #15).** Per-minute drill (2026-06-11): the live STREAM captured only **~10 of 50 names** for the entire day until ~15:51 ET, when the subscription scaled 10→50; backfill had all 50. So the headline n_trades-within-2% 98.05% / **sign 99.82%** is essentially a **10-NAME proof + a ~10-minute 50-name window (15:51-16:00)**, not a 50-name full-session proof. Where the stream DID capture a name, parity is excellent — 15:30/15:45/15:55 all 100% count+sign (the overnight/last-cadence anchor is CLEAN; verdict not tainted). Residual threats: (a) **16:00-ET closing-print minute = 14% within-2%** (closing-auction divergence) → exclude ≥16:00; Modeller's ≥15:50 OFI line is safe. (b) **backfill trade-agg is RTH-bounded** (no post-16:00 ET) → post-close OFI has NO backfill to validate against. | OPEN — the 10→50 coverage cause is RESOLVED (prod: it's the 7dfb438 deploy restart at 19:51:04Z, a one-off, NOT systematic loss). Ingestor has run continuously since, so **2026-06-12 is the first full-50 settled-day candidate** — re-run #15 on the settled data AFTER today's close. |
| P2 | denylist-regex-dependence | The frozen known-fund denylist (5,284) and `is_etf_like` are both NAME-regex derived, so a fund whose name evades the regex slips BOTH the builder and the denylist. True independence needs an external issuer/share-class metadata source (Alpaca classes stock & ETF alike as us_equity — no broker-side split). | OPEN — tracked upgrade: wire an external fund/share-class signal; until then the denylist is necessary-not-sufficient |
| P0 | etf-contamination | ~207 of 1000 universe members (~21%) are ETFs/ETNs/leveraged-inverse/VIX-futures funds (SOXL, TQQQ, SQQQ, TNA, UVXY, VXX, UPRO, SPXU, TSLL...), NOT single-name equities. They reached the feature panel (1.59M feature_vector rows / 207 symbols) and were RANKED cross-sectionally against stocks. The price-only "no edge" verdict was drawn on this contaminated cross-section -> NOT trustworthy until re-run clean. **ROOT CAUSE:** the ETF filter `is_etf_like` EXISTS and is wired into the executor (main.py:112) but is NEVER called in `quantlib.universe.select_universe` — the function that builds universe_membership. Filter applied at order layer, skipped at research/universe layer (asymmetry hid it: live trading clean, offline panel polluted). **REAL FIX:** thread asset name into SymbolStats + filter `is_etf_like` in select_universe (clean on every rebuild) — not the one-off SQL exclusion (band-aid). Classifier + clean scaling list staged in scripts/etf_exclusion.sql. | PARTIAL — select_universe fixed (814e548); universe rebuilt clean & now **AUTOMATED-GREEN on the live universe** (`universe_is_equities_only` + independent `universe_no_known_funds`, 0/1000 at 2026-06-12, was 209). Remaining: clean panel (v1.1.1) + full history (tasks #2/#12), then RE-RUN the battery (task #4). |
| P1 | UTC-calendar-legacy | UTC-calendar leakage now AUTOMATED (`calendar_et_correct`) and LOCALIZED: 30,970 v1.0.0-historical + 1,250 v1.0.0-stream rows; **active v1.1.0 and v1.1.1 are 0/ET-clean.** The serving path is clean; the dirt is confined to the deprecated v1.0.0 set. | OPEN-LOW — purge v1.0.0 to clear the legacy red; active sets gated green |
| P1 | warmup-unmonitored | now AUTOMATED (`warmup_coverage`): ragged-warmup + dead-feature detector, gated on the active set. Confirms v1.0.0's 5 micro features are 99.5% NaN (dead); active set carries none. | MITIGATED — monitored fail-loud; build-time warmup assert still desirable (defense in depth) |
| P1 | preds-degenerate | predictions ~80% within 1bp of 0 → basket was tie-break noise | MITIGATED — executor degeneracy guard added; preds still non-tradeable |
| P1 | pit-leak | now AUTOMATED (`pit_universe_membership`): the leak is far bigger than the earlier ~14 estimate — **117,047 (symbol,date) feature rows in v1.1.0 are not in-universe members for that date, across 217 symbols of which 209 (96%) are the ETFs.** The panel was built over a current symbol set across all history, not strict PIT membership. Resolves when the clean v1.1.1 panel + history land (tasks #2/#12). | OPEN — gated on active set; will go green when v1.1.1 history is PIT-correct |
| P2 | view-fanout | training_data 2× horizon fan-out | LOW — trainer filters horizon; harden the view |

## Unprovoked creative probes (Ben's directive — log every probe + result, even clean)

- **2026-06-12 #1 — Execution loop end-to-end consistency (orders→fills→pnl→recon).** NOT
  CLEAN. Found `exec-recon-one-directional` (see open concerns, P1): the 6/12 live basket
  filled 2L+1S of an intended 3L/3S (KEEL/FLY/AMPX never filled) yet reconciliation reported
  `ok:true` all session because it only checks for *unexpected* broker positions, never for
  submitted-but-unfilled. Drivers: fixed-1¢ marketable-limit (doesn't scale to price/spread)
  + terminal-blind `orders_log.status`. Reported to Manager; fix owned by Exec/Risk; QA to add
  a `fill_reconciliation` invariant after the fix lands.

## Data provenance facts (encode so we don't re-derive them)

- **Intraday bars/features (v1.1.x panel)** come from BACKFILL fetched in month windows →
  vulnerable to mixed split-adjustment states (the KLAC class, task #17). Detect with
  `no_extreme_backfill_jump`.
- **Overnight labels** come from a SEPARATE one-shot, fully **SPLIT-ADJUSTED** fetch → the KLAC
  backfill-deflation does NOT touch them. Impact of the 11-name split artifact is momentum-only,
  ~0.03% of panel cells, **labels unaffected** — battery verdict closes as CAVEATED pending the
  Modeller's 11-name sensitivity pass.

## KNOWN / SCHEDULED — do NOT re-flag as findings

- **Ingestor BAR subscription still carries the contaminated 1000-name membership** (incl.
  ETFs) until the post-close clean-membership restart (batched with prod #11 after 2026-06-12
  16:00 ET). HARMLESS: ETF bars are stored but unused by trading/universe. Known + scheduled.
- **Trade-agg stream 10→50 transition at 15:51 ET 6/11** = the 7dfb438 deploy restart, a
  one-off (not systematic). Coverage-mismatch concern closed.

## Resolved (kept for history)

- Compression: 0/74 → 68/74 chunks (DB 6.8GB→2.7GB).
- day_of_week ET-correct across all 662k historical rows; per-ts demean exact; no Inf.
- Micro features 99.9% NaN universe-wide → dropped from the v1.1.0 set (identity-leak risk).
