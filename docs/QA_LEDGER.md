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
- **`tests/fixtures/live_feature_coverage_baseline.json`** — trailing per-day live-coverage rows.

**Tiering (2026-06-12, fixes the P2 `suite-too-slow`):** the all-invariants run exceeds 500s on
the 5.5M-row panel. `--fast` = FAST_INVARIANTS (universe composition trio + live_feature_coverage)
as the every-wake / post-close standalone gate; `--full` adds the heavy panel scans (parity/warmup/
pit/inf/jump/bars) for the nightly run. Default (no flag) still runs all; `--only`/pytest unchanged.
NOTE: the fast tier wall-time is ~38s NOT ~3s — dominated by `docker compose exec` subprocess spawn
PER query (user-cpu is <1s). A future optimization is batching queries / a persistent psql conn;
38s is still a runnable standalone gate (vs the 500s+ monolith). The post-close daily run uses `--fast`.

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
| `live_feature_coverage` | I4-live | **NEW (Ben's 2026-06-12 ask "how many features have values for today?").** Same-day source='live' per-FAMILY valued% vs DERIVED expectation: price/vol ≥ warmup-adequate fraction−slack; trade/quote ≥ captured-name fraction−slack; calendar exact 100%; symbol deficit EXPLAINED by warmup, not silent. Fails on a family DROP vs trailing baseline (>5%) or unexplained symbol-count loss. The live serving path was previously UNCHECKED on the day it's produced — only the research panel was scoped. Baseline: tests/fixtures/live_feature_coverage_baseline.json, rolled via `--update-baseline` post-close. |

**First baseline row (2026-06-12, recorded):** live v1.0.0, 9,098 rows / 788 syms / 12 cadences;
price/vol 90.1% (warmup ceiling 78.8% = 788/1000 names ≥60 bars — the 212-name deficit is fully
warmup-explained), calendar 100%, trade/quote 6.6% (= 50 captured / 788 live, ≈ expected 6.3%).
The 6.6% trade/quote is the M2 signal to watch: it should JUMP as capture scales 50→500; a stall
there is a capture regression the invariant will catch.

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
| P1 | parity-not-split-aware | **NEW (verifying 449bcf2, 2026-06-12 ~12:50 ET PT): the `backfill_realtime_parity` invariant is NOT split-aware, so every corporate action permanently inflates it with non-bug noise.** Independently re-ran parity post-#17: **1.08%** (was 1.14%), still RED. Per-symbol drill of the 7,732 mismatches: **KLAC = 833 bars at max_rel 9.01× (has_split=TRUE)** — this is the raw-stream-vs-split-ADJUSTED-backfill gap on KLAC's pre-ex-date days (6/10 stream $2138 vs adj backfill $214; 6/11 $2325 vs $233; 6/12-onward stream==backfill==$244.83, 0% mismatch). It is CORRECT behavior, not a bug — stream records as-traded, backfill is retroactively split-adjusted, so they're incomparable across a split. The OTHER top drivers (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS, ~6,000 bars) are has_split=FALSE at max_rel 0.00-0.01 = the systematic <1% close-methodology offset (official/consolidated close vs last-trade-minute). **Removing KLAC's split contribution: (7732−833)/714835 = 0.97% — UNDER the 1% gate.** So the parity check needs the SAME split-awareness the jump check just got (449bcf2): exclude (symbol, pre-ex-date) overlaps for names with a split in corporate_actions, else the 1% gate becomes a split-FREQUENCY function, not a data-quality signal, and KLAC stays falsely RED forever. | OPEN — proposed FIX (qa, M2): make `check_backfill_realtime_parity` self-gate vs corporate_actions exactly like `_split_ex_dates()` does (exclude stream bars on/before a name's split ex-date from the mismatch count). Then KLAC clears and the residual ~0.97% is purely the close-methodology offset (prod's canonical-close call). DOES NOT block the KLAC denylist pull — that gate is met on the CORRECT criterion (backfill internally consistent + post-ex-date overlap exact), verified below. |
| P2 | jump-check-oom | **NEW (verifying 449bcf2): the full-depth `no_extreme_backfill_jump` ERRORS, not passes, at full history** — `psql: out of shared memory / increase max_locks_per_transaction`. It scans the full 253M-row bars_1m hypertable across all (largely uncompressed) chunks and exhausts Postgres locks. My SCOPED run (`QA_JUMP_SINCE=2026-05-01`) PASSES cleanly and confirms KLAC's 6/01 step is gone + self-gating works; but the commit's headline "GREEN across 785 names / full history" can't be reproduced unscoped on this DB without tuning. | OPEN-LOW — FIX (qa): chunk the scan by year, or raise max_locks_per_transaction, or default QA_JUMP_SINCE to the panel start. The CONCLUSION (KLAC resolved, 0 unexplained recent) is verified via the scoped run; only the full-history one-shot is unrunnable. |
| P1 | exec-recon-one-directional | **NEW (unprovoked probe, 2026-06-12 ~12:48 ET): the live execution loop silently runs an UNBALANCED basket and reconciliation reports it CLEAN.** `reconcile()` (services/executor/main.py:315-330) flags ONLY *unexpected* broker positions (a symbol at the broker we didn't submit). It is structurally blind to the INVERSE — orders we submitted that REJECTED, never filled, or partial-filled. Evidence: 6/12 intended a market-neutral 3L/3S basket (KEEL/SATS/UUUU long, FLY/W/AMPX short); broker holds only SATS+1/UUUU+13/W−2 → **2L+1S actually filled, KEEL+FLY+AMPX NEVER FILLED**, yet every `reconciliation_log` row all day reads `ok:true, unexpected:[]`. The book is net long-skewed vs market-neutral intent and NO monitor caught it. ROOT CAUSE of the misses: **fixed $0.01 marketable-limit cross** (every 6/12 order crosses NBBO by exactly 1¢ regardless of price/spread) — fine on W@78/SATS@126 (~1bp, filled), too thin on KEEL@5.74/FLY@37.26/AMPX@16.99 where 1¢ vs the spread/quote-drift left them non-marketable (FLY limit 37.26 was already INSIDE bid 37.27 at submit). SECOND defect: `orders_log.status` is terminal-blind — only ever 'intended'→'submitted', never filled/rejected/canceled written back (main.py:157,264), so the DB ledger alone can't tell whether a submit worked (all-time orders_log filled=0 despite real fills). Impact: today's P&L is noise so harmless NOW, but once a real-edge model is live this silently corrupts every basket's neutrality and the M4 paper track record — the ETF failure-mode (false confidence) reproduced in the EXECUTION lane. | OPEN — FIX owned by Exec/Risk: (1) symmetric reconcile = also flag submitted-but-unfilled / partial / orphaned-open orders, (2) price/spread-scaled marketable-limit (not fixed 1¢), (3) write terminal status back to orders_log. QA will encode a `fill_reconciliation` invariant (submitted set == filled+accounted set per day) once Exec lands the fix. |
| P1 | parity-1.14 | `backfill_realtime_parity` = 1.14% (7,731/678k bars >0.2% close diff). **DRILLED (task #14, scripts/parity_drill.sql), two drivers:** (1) **KLAC: a persistent EXACTLY-10× divergence** (stream 2312.47 vs backfill 231.01) across BOTH days; all 833 KLAC bars = the entire >10% band (~11% of mismatches). **⚠️ DIRECTION CORRECTED (prod deep-dive vs Alpaca live ~2429): the BACKFILL is 10×-DEFLATED, the STREAM was CORRECT.** Root cause: a split landing mid-backfill left pre-split months fetched under the OLD adjustment basis (mixed split-adjustment states). The drill's *detection* of the 10× gap was right; my *attribution* (stream bug) was backwards. (2) **~87% are small (<1%) systematic per-symbol offsets** — ~15-20 symbols (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS…) mismatch 100% of their bars by <1%, i.e. a methodological close-price difference (official/consolidated close vs last-trade minute close), not random tick loss. Even after fixing KLAC the residual is ~1.02%. | OPEN — driver breakdown committed; FIX owned by prod-architect (KLAC 10× + canonical-close decision). Gate stays 1% (catching real issues). |
| P1 | backfill-split-jump | **NEW class (task #17): mixed split-adjustment states in backfill** — a split landing mid-backfill leaves pre-split months on the old adjustment basis, deflating prices (KLAC = confirmed 10× artifact). A >3× day-jump sweep flagged **11 names**; KLAC confirmed artifact, the other 10 are likely real moves/reverse-splits (prod verifying each). This is a price-integrity bug that reaches the PANEL (momentum features), not just the parity overlap. | **RESOLVED (2026-06-12).** Prod #17 re-fetched KLAC (one Adjustment.ALL pass) → `no_extreme_backfill_jump` re-run is **GREEN (0 unexplained jumps across 785 names)**; I confirmed the denylist-removal condition to prod/exec. The invariant now **SELF-GATES against the live #18 corporate_actions table** (216 split (symbol,date±1) entries; real splits auto-exempt, KLAC's 10:1 ex-6/12 verified present) with the manual allowlist as fallback for non-split events (IPOs/restructurings — the 13 real movers). COMPOSES with prod's complementary current-deflation check (backfill close vs fresh quote ~5%): theirs = current state cheaply, mine = historical mid-series steps at depth. Standing invariant in the `--full` nightly tier. |
| P1 | tradeagg-not-at-scale | **The "at-scale" trade-agg parity proof is NOT YET REAL (task #15).** Per-minute drill (2026-06-11): the live STREAM captured only **~10 of 50 names** for the entire day until ~15:51 ET, when the subscription scaled 10→50; backfill had all 50. So the headline n_trades-within-2% 98.05% / **sign 99.82%** is essentially a **10-NAME proof + a ~10-minute 50-name window (15:51-16:00)**, not a 50-name full-session proof. Where the stream DID capture a name, parity is excellent — 15:30/15:45/15:55 all 100% count+sign (the overnight/last-cadence anchor is CLEAN; verdict not tainted). Residual threats: (a) **16:00-ET closing-print minute = 14% within-2%** (closing-auction divergence) → exclude ≥16:00; Modeller's ≥15:50 OFI line is safe. (b) **backfill trade-agg is RTH-bounded** (no post-16:00 ET) → post-close OFI has NO backfill to validate against. | OPEN — the 10→50 coverage cause is RESOLVED (prod: it's the 7dfb438 deploy restart at 19:51:04Z, a one-off, NOT systematic loss). Ingestor has run continuously since, so **2026-06-12 is the first full-50 settled-day candidate** — re-run #15 on the settled data AFTER today's close. |
| P2 | denylist-regex-dependence | The frozen known-fund denylist (5,284) and `is_etf_like` are both NAME-regex derived, so a fund whose name evades the regex slips BOTH the builder and the denylist. True independence needs an external issuer/share-class metadata source (Alpaca classes stock & ETF alike as us_equity — no broker-side split). | OPEN — tracked upgrade: wire an external fund/share-class signal; until then the denylist is necessary-not-sufficient |
| P0 | etf-contamination | ~207 of 1000 universe members (~21%) are ETFs/ETNs/leveraged-inverse/VIX-futures funds (SOXL, TQQQ, SQQQ, TNA, UVXY, VXX, UPRO, SPXU, TSLL...), NOT single-name equities. They reached the feature panel (1.59M feature_vector rows / 207 symbols) and were RANKED cross-sectionally against stocks. The price-only "no edge" verdict was drawn on this contaminated cross-section -> NOT trustworthy until re-run clean. **ROOT CAUSE:** the ETF filter `is_etf_like` EXISTS and is wired into the executor (main.py:112) but is NEVER called in `quantlib.universe.select_universe` — the function that builds universe_membership. Filter applied at order layer, skipped at research/universe layer (asymmetry hid it: live trading clean, offline panel polluted). **REAL FIX:** thread asset name into SymbolStats + filter `is_etf_like` in select_universe (clean on every rebuild) — not the one-off SQL exclusion (band-aid). Classifier + clean scaling list staged in scripts/etf_exclusion.sql. | PARTIAL — select_universe fixed (814e548); universe rebuilt clean & now **AUTOMATED-GREEN on the live universe** (`universe_is_equities_only` + independent `universe_no_known_funds`, 0/1000 at 2026-06-12, was 209). Remaining: clean panel (v1.1.1) + full history (tasks #2/#12), then RE-RUN the battery (task #4). |
| P1 | UTC-calendar-legacy | UTC-calendar leakage now AUTOMATED (`calendar_et_correct`) and LOCALIZED: 30,970 v1.0.0-historical + 1,250 v1.0.0-stream rows; **active v1.1.0 and v1.1.1 are 0/ET-clean.** The serving path is clean; the dirt is confined to the deprecated v1.0.0 set. | OPEN-LOW — purge v1.0.0 to clear the legacy red; active sets gated green |
| P1 | warmup-unmonitored | now AUTOMATED (`warmup_coverage`): ragged-warmup + dead-feature detector, gated on the active set. Confirms v1.0.0's 5 micro features are 99.5% NaN (dead); active set carries none. | MITIGATED — monitored fail-loud; build-time warmup assert still desirable (defense in depth) |
| P1 | preds-degenerate | predictions ~80% within 1bp of 0 → basket was tie-break noise | MITIGATED — executor degeneracy guard added; preds still non-tradeable |
| P1 | pit-leak | now AUTOMATED (`pit_universe_membership`): the leak is far bigger than the earlier ~14 estimate — **117,047 (symbol,date) feature rows in v1.1.0 are not in-universe members for that date, across 217 symbols of which 209 (96%) are the ETFs.** The panel was built over a current symbol set across all history, not strict PIT membership. Resolves when the clean v1.1.1 panel + history land (tasks #2/#12). | OPEN — gated on active set; will go green when v1.1.1 history is PIT-correct |
| P2 | view-fanout | training_data 2× horizon fan-out | LOW — trainer filters horizon; harden the view |
| P2 | suite-too-slow | **The invariant suite no longer completes as a single gate** — full `qa_invariants.py` exceeds 500s on the 5.5M-row v1.1.1 panel (bars_integrity ~50s + no_inf ~23s + calendar ~32s + jump ~77s + the 4 unmeasured heavy scans parity/trade_agg/pit/warmup). A fail-loud suite that's too slow to run defeats its own purpose (the role's core thesis). Per-invariant `--only` works fine; the all-in-one run times out. | MITIGATED 2026-06-12 — `--fast`/`--full` tiering shipped: fast tier (universe trio + live_feature_coverage) is the every-wake/post-close standalone gate, full tier (panel scans) nightly. Residual: fast wall-time ~38s is `docker compose exec`-per-query spawn overhead (user-cpu <1s) — future opt = batched queries / persistent conn. Runnable now. |

## Unprovoked creative probes (Ben's directive — log every probe + result, even clean)

- **2026-06-12 #1 — Execution loop end-to-end consistency (orders→fills→pnl→recon).** NOT
  CLEAN. Found `exec-recon-one-directional` (see open concerns, P1): the 6/12 live basket
  filled 2L+1S of an intended 3L/3S (KEEL/FLY/AMPX never filled) yet reconciliation reported
  `ok:true` all session because it only checks for *unexpected* broker positions, never for
  submitted-but-unfilled. Drivers: fixed-1¢ marketable-limit (doesn't scale to price/spread)
  + terminal-blind `orders_log.status`. Reported to Manager; fix owned by Exec/Risk; QA to add
  a `fill_reconciliation` invariant after the fix lands.

## #15 full-50 coverage PRE-CONFIRMED (2026-06-12 live, before settle)

Checked trade_agg_1m live coverage today: **all 50 names captured every hour 04:00→12:00 ET**
(50 distinct symbols/hour, ~3000 rows/hr = 50×~60min, uninterrupted). Unlike 6/11 (10-name
until 15:51), 6/12 is a genuine full-50 session from the pre-market on. So tonight's #15 run on
the settled 6/12 data is a REAL at-scale proof, not a 10-name proxy — premise confirmed before
running. Reminders from the 6/11 drill still apply: EXCLUDE the 16:00 ET closing-auction minute
(14% within-2% — auction divergence) and note backfill trade-agg is RTH-bounded (no post-16:00
validation target).

## KLAC re-fetch verification baseline (captured 2026-06-12 12:49 ET, BEFORE prod #17 re-fetch)

Pre-re-fetch backfill state to diff against tonight: 217,382 bars, close range 188.65–2097.50.
The artifact is a clean **~9.95× DOWN-step on 2026-06-01**: 5/29 daily-avg close 1933.98 →
6/01 192.25 (and 6/02..6/12 stay in the ~190–240 band). The real KLAC 10:1 split ex-date is
**6/12**, so 6/01–6/11 backfill is deflated ~10× — those month-windows were fetched on the
post-split adjustment basis while late-May kept the pre-split basis (mixed-basis confirmed,
boundary = 6/01). **#14/#17 GREEN CONDITION after re-fetch:** KLAC backfill is internally
consistent on ONE basis (no 9.95× step at 6/01), `no_extreme_backfill_jump` stops flagging
KLAC, and parity overlap for KLAC closes. That green is the GATE for exec pulling the KLAC
denylist — do NOT signal it until verified.

## Reviews performed (mapped-reviewer gate outcomes — policy docs/REVIEW_POLICY.md)

- **2026-06-12 — 449bcf2 (original qa's KLAC-resolved + corp-actions self-gating), verified by qa-2.**
  ABSORBED + independently VERIFIED. Coexists cleanly with my tiering (245195c) and live_feature_
  coverage — no clobber; they correctly placed the heavy jump check in the --full tier. Checks I ran
  myself (not trusting the commit): (1) KLAC backfill re-fetch CONFIRMED — daily avg 5/29 went 1933.98→
  193.40, the 9.95× mixed-basis step at 6/01 is GONE, series now single-basis. (2) corporate_actions
  table real: 72 split entries (42 fwd / 19 rev / 11 stock-div); KLAC's 10:1 forward_splits ex-2026-06-12
  PRESENT → auto-exempt works. (3) scoped jump check (QA_JUMP_SINCE=2026-05-01) PASS. **2 corrections to the
  commit's claims:** (a) it says "216 split entries" — that's the ±1-day EXPANDED tuple count; the table has
  **72** split rows (216 = 72×3). Harmless, code is right. (b) the full-history jump run does NOT pass — it
  ERRORS on Postgres locks (see P2 jump-check-oom); the GREEN is proven by the scoped run, not the unscoped
  one. **KLAC denylist-removal data-integrity condition = MET** (backfill internally consistent + post-ex-date
  overlap exact); the residual parity RED is the separate split-awareness gap (P1 parity-not-split-aware),
  NOT a KLAC backfill problem — so it does not block the pull.

- **2026-06-12 — #19 exec symmetric-reconcile fix (b856aa7), reviewer=qa.** APPROVE w/ 2
  must-fix conditions before deploy. The fix genuinely closes my exec-recon-one-directional
  finding (sees partial/unfilled/rejected/orphaned + writes terminal status/filled_qty back).
  Conditions: (1) `basket_neutral` is a NAME-COUNT check, not notional — add per-side filled
  NOTIONAL (filled_qty×price) so dollar-skew is visible (a 1-sh $5 long vs 100-sh $300 short
  passes count-neutral); my fill_reconciliation invariant needs this for realized-neutrality.
  (2) fill_ts double-count: a partial ending `done_for_day`/`expired` has neither filled_at
  nor canceled_at → falls to mutating `updated_at` → 2 fills_log rows/order = double-counted
  P&L. Partials are routine here (4/6 fills on 6/11 were partial). Fix: deterministic fill_ts
  or key the upsert on alpaca_order_id alone (cumulative). Agreed (3): per-cycle `ok` stays
  unexpected+rejected only (no flap); the HARD incomplete-fill/lopsided gate lives in MY
  per-day invariant, evaluated post-flatten when all orders are terminal. Deploy gated on my
  re-confirm of the updated diff + prod-architect-2 + Manager. SEPARATE from the KLAC denylist
  parity gate (that waits on tonight's re-fetch).
  - **RE-REVIEW 2026-06-12 (fix 8ba6c89, exec): GREEN.** Both conditions correctly landed.
    (1) detail now carries filled_long_notional/filled_short_notional/net_notional, sourced
    live from `order.filled_avg_price` (accurate for mid-session partials, no fills_log
    join-fanout) — my per-day invariant asserts realized dollar-neutrality on net_notional.
    (2) fill_ts fallback now `filled_at or canceled_at or submitted_at` — submitted_at is
    immutable (the branch only fires on filled_qty>0 ⇒ order was accepted ⇒ submitted_at
    non-null), so one fills_log row/order, double-count class closed. filled_qty written even
    when 0 (distinguishes terminal-0-fill from never-synced). Single file, no schema change.
    My approval given; deploy still needs prod-architect-2 + Manager bless → rides the one
    post-flatten executor rebuild. POST-DEPLOY I OWN: build the `fill_reconciliation` per-day
    invariant against this schema + verify new recon rows carry the rich detail (before-
    baseline captured: 1,557 rows today all ok:true, no new fields).

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
