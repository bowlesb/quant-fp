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
  edge candidate). **NOW PROVEN AT SCALE (2026-06-12 first full-50 settled-day, task #15):
  count 99.79% / SIGN 99.85% / signed-vol 99.41% within 2%, with stream==backfill 50 names
  every hour all session.** The gate ("settled-day trade-agg parity at scale before any trade
  feature enters a trusted model") is MET at the current 50-name scale. Threat (2) tick-rule
  sign — the one I worried most about — holds at 99.85%. Re-prove as scale grows 50→500.
  Standing caveat: backfill trade-agg is RTH-bounded → keep OFI ≤15:59 ET (no post-close
  backfill to validate against; 16:00 auction-minute dips to 95.5%).
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

**FULL-SUITE evidence (2026-06-13, FIRST run without lock-OOM after the max_locks 64→2048 bump):**
10 PASS / 3 FAIL. The two parity invariants are GREEN at FULL-PANEL scale for the first time:
`backfill_realtime_parity` **99.32% within 0.2%** on 1,020,258 overlap bars (the split-aware 0.97%
residual fix holds at scale), `trade_agg_parity` PASS, `pit_universe_membership` + `calendar_et_correct`
PASS on the active set. The 3 FAILs:
- **fill_reconciliation** — KNOWN 6/12 true-positive (3L/1S, 52% net exposure), resolves Monday.
- **warmup_coverage — NEW, P1-for-M3 (NOT a live-trading incident).** The active set auto-selected to
  **v1.2.0** (the OFI/order-flow research set, 1,516 rows / **50 names** / source='historical' ONLY —
  confirmed NOT live-served; model-server consumes the trained set's version with source='live', which
  is still v1.0.0). v1.2.0 has **6 genuinely-DEAD features: the daily-momentum family mom_3d/5d/10d +
  _rel variants (idx 15-21), 100% NaN in BOTH early AND late window** (so not warmup — a real compute
  failure). Almost certainly the 50-name OFI panel build didn't join the daily-bar history those
  multi-day momentum features need. Impact: if an OFI model is trained on v1.2.0, these 6 silently
  contribute nothing (the I4 silent-NaN-degrade failure mode the invariant exists to catch). Routes to
  MODELLER (feature defs) / prod (panel build): either drop the momentum family from v1.2.0's
  registration or fix the daily-bar join. Live trading unaffected.
- **no_extreme_backfill_jump — NEW: STI 2025-10-13 5.6→21.55 (3.85×), 1 unexplained.** Diagnosed: STI
  has a reverse_splits 50:1 but on 2025-05-12 (5mo EARLIER, not this date), so the check correctly did
  NOT auto-exempt it. Daily closes 5.51(10/10)→16.20(10/13)→23.10(10/14) with bar-count stepping
  147→764/day = sustained new level + rising activity → most likely a REAL microcap move (post-reverse-
  split $5 name running on volume), NOT a mixed-basis artifact (which spikes-and-reverts). prod's call
  (owns backfill + the manual allowlist): manual-allowlist if confirmed real, re-fetch if artifact.

## Open concerns — severity-ranked (update status each wake)

| sev | id | concern | status |
|-----|----|---------|--------|
| P1 | parity-not-split-aware | **NEW (verifying 449bcf2, 2026-06-12 ~12:50 ET PT): the `backfill_realtime_parity` invariant is NOT split-aware, so every corporate action permanently inflates it with non-bug noise.** Independently re-ran parity post-#17: **1.08%** (was 1.14%), still RED. Per-symbol drill of the 7,732 mismatches: **KLAC = 833 bars at max_rel 9.01× (has_split=TRUE)** — this is the raw-stream-vs-split-ADJUSTED-backfill gap on KLAC's pre-ex-date days (6/10 stream $2138 vs adj backfill $214; 6/11 $2325 vs $233; 6/12-onward stream==backfill==$244.83, 0% mismatch). It is CORRECT behavior, not a bug — stream records as-traded, backfill is retroactively split-adjusted, so they're incomparable across a split. The OTHER top drivers (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS, ~6,000 bars) are has_split=FALSE at max_rel 0.00-0.01 = the systematic <1% close-methodology offset (official/consolidated close vs last-trade-minute). **Removing KLAC's split contribution: (7732−833)/714835 = 0.97% — UNDER the 1% gate.** So the parity check needs the SAME split-awareness the jump check just got (449bcf2): exclude (symbol, pre-ex-date) overlaps for names with a split in corporate_actions, else the 1% gate becomes a split-FREQUENCY function, not a data-quality signal, and KLAC stays falsely RED forever. | **RESOLVED (2026-06-12).** `check_backfill_realtime_parity` now split-aware via `_split_cutoffs()`: excludes a split name's stream overlap on/before its latest split ex_date. Parity **PASS at 0.97%** (was FAIL 1.08%); 55 split names excluded, KLAC's 833 bars gone (overlap 714,835→712,655, mismatch 7,732→6,899). **CRITICAL note (so a future reviewer doesn't re-invert): exemption is CORRECT here but was BACKWARDS in the jump check — different semantics.** Parity = RAW stream vs RETRO-ADJUSTED backfill → pre-ex overlap incomparable BY DESIGN (exclude = remove non-bug). Jump check = backfill INTERNAL consistency → jump AT ex-date = failed adjustment (exclude = hide the bug). Do NOT make one match the other. ACCEPTED RESIDUAL: a real pre-ex corruption on a split name now hides from parity; COVERED BY (a) `no_extreme_backfill_jump` (internal break caught regardless) + (b) the same name's POST-ex overlap still fully checked (KLAC 6/12-on = 0%). Residual 0.97% = purely the close-methodology offset (prod's canonical-close call), uncontaminated by splits. |
| P2 | db-locks-headroom | **NEW (2026-06-12, hit THREE times now): wide multi-partition analytical queries ERROR with `out of shared memory / increase max_locks_per_transaction`** — the DB's lock pool can't take a query that locks the 253M-row bars_1m hypertable's many (largely uncompressed) chunks AND/OR the 613 `labels` child partitions at once. Confirmed blockers: (a) full-history `no_extreme_backfill_jump`; (b) full-depth `backfill_realtime_parity` per-symbol drill; (c) the ex-div label-hygiene bars-join (827f478 verify). Each works SCOPED (narrow ts window / single symbol) but not full-panel. This will worsen as the panel grows and bites the `--full` nightly suite. | **MITIGATED (2026-06-13).** prod raised `max_locks_per_transaction` 64→**2048** (confirmed live: `SHOW` = 2048, source=postgresql.auto.conf via ALTER SYSTEM). The lock-OOM trigger is REMOVED — my `--full` suite this wake ran the full-panel `backfill_realtime_parity` scan to completion without the OOM error that blocked it before. Same fix that unblocks task #6's experimenter-poisoning. RESIDUAL (reproducibility, P3): the 2048 setting lives in the mounted `./data/pg` volume (survives restart/recreate) but is NOT declared in docker-compose.yml — a fresh env / wiped volume starts at default 64 and the lock-OOM returns until someone re-runs ALTER SYSTEM. Flagged to prod: add `command: postgres -c max_locks_per_transaction=2048` to compose so it's declarative. Not urgent (volume persists). |
| P1 | exec-recon-one-directional | **NEW (unprovoked probe, 2026-06-12 ~12:48 ET): the live execution loop silently runs an UNBALANCED basket and reconciliation reports it CLEAN.** `reconcile()` (services/executor/main.py:315-330) flags ONLY *unexpected* broker positions (a symbol at the broker we didn't submit). It is structurally blind to the INVERSE — orders we submitted that REJECTED, never filled, or partial-filled. Evidence: 6/12 intended a market-neutral 3L/3S basket (KEEL/SATS/UUUU long, FLY/W/AMPX short); broker holds only SATS+1/UUUU+13/W−2 → **2L+1S actually filled, KEEL+FLY+AMPX NEVER FILLED**, yet every `reconciliation_log` row all day reads `ok:true, unexpected:[]`. The book is net long-skewed vs market-neutral intent and NO monitor caught it. ROOT CAUSE of the misses: **fixed $0.01 marketable-limit cross** (every 6/12 order crosses NBBO by exactly 1¢ regardless of price/spread) — fine on W@78/SATS@126 (~1bp, filled), too thin on KEEL@5.74/FLY@37.26/AMPX@16.99 where 1¢ vs the spread/quote-drift left them non-marketable (FLY limit 37.26 was already INSIDE bid 37.27 at submit). SECOND defect: `orders_log.status` is terminal-blind — only ever 'intended'→'submitted', never filled/rejected/canceled written back (main.py:157,264), so the DB ledger alone can't tell whether a submit worked (all-time orders_log filled=0 despite real fills). Impact: today's P&L is noise so harmless NOW, but once a real-edge model is live this silently corrupts every basket's neutrality and the M4 paper track record — the ETF failure-mode (false confidence) reproduced in the EXECUTION lane. | **MITIGATED+MONITORED (2026-06-12).** Exec shipped & deployed #19 (symmetric reconcile + spread-scaled limit + terminal-status writeback); I VERIFIED LIVE (reconciliation_log now carries intended/filled/net_notional/unfilled — see Reviews). QA side DONE: built the **`fill_reconciliation` invariant** (FAST tier) — post-flatten, asserts no stuck order + realized L/S net-exposure ≤40% of gross (size-INDEPENDENT, not abs-$ — caught my own first-draft bug where $-threshold passed a fully one-sided tiny basket) + fill-rate ≥60%. It correctly FAILS on today's 6/12 book (3L/1S, **52% net exposure** — the lopsided fill the old reconcile reported ok:true on). This FAIL is a TRUE POSITIVE surfacing the fixed-1¢-limit fill problem; goes green once #19's spread-scaled limit fills both legs. Standing monitor now exists so this can never silently regress. **CROSS-LANE TENSION (exec root-caused, ledger d2efa8f): unfilled shorts were wide-spread microcaps (FLY 112bps / AMPX 82bps); #19's 0.5×spread cross will fill them — but filling a 112bps name COSTS ~½ spread (~56bps one-way). So the fill-neutrality gate and the net-of-cost edge goal PULL AGAINST each other precisely on the hardest-to-fill names. Not a flaw in either gate — a real trade-off the slippage program (execution_slippage view) must quantify. WATCH: if the strategy systematically shorts wide-spread microcaps, "market-neutral" costs real bps; a future portfolio-construction rule may need to cap short-leg spread / down-weight high-spread names. Monday's open = first live test; exec loops me + modeller-2 with actual fills+slippage.** |
| P1 | parity-1.14 | `backfill_realtime_parity` = 1.14% (7,731/678k bars >0.2% close diff). **DRILLED (task #14, scripts/parity_drill.sql), two drivers:** (1) **KLAC: a persistent EXACTLY-10× divergence** (stream 2312.47 vs backfill 231.01) across BOTH days; all 833 KLAC bars = the entire >10% band (~11% of mismatches). **⚠️ DIRECTION CORRECTED (prod deep-dive vs Alpaca live ~2429): the BACKFILL is 10×-DEFLATED, the STREAM was CORRECT.** Root cause: a split landing mid-backfill left pre-split months fetched under the OLD adjustment basis (mixed split-adjustment states). The drill's *detection* of the 10× gap was right; my *attribution* (stream bug) was backwards. (2) **~87% are small (<1%) systematic per-symbol offsets** — ~15-20 symbols (SPYM/ALB/WMB/GPN/NDAQ/CB/AMT/ADP/BR/DKS…) mismatch 100% of their bars by <1%, i.e. a methodological close-price difference (official/consolidated close vs last-trade minute close), not random tick loss. Even after fixing KLAC the residual is ~1.02%. | SUPERSEDED by [parity-not-split-aware → RESOLVED]: KLAC's 833-bar contribution is the raw-vs-adjusted split artifact, now excluded by `_split_cutoffs()` → parity PASSES at 0.97%. The residual 0.97% is the ~15-20 small per-symbol close-methodology offsets (SPYM/ALB/WMB…), which is the REAL open question — prod's canonical-close (official/consolidated vs last-trade-minute) decision. Gate stays 1%. |
| P1 | backfill-split-jump | **NEW class (task #17): mixed split-adjustment states in backfill** — a split landing mid-backfill leaves pre-split months on the old adjustment basis, deflating prices (KLAC = confirmed 10× artifact). A >3× day-jump sweep flagged **11 names**; KLAC confirmed artifact, the other 10 are likely real moves/reverse-splits (prod verifying each). This is a price-integrity bug that reaches the PANEL (momentum features), not just the parity overlap. | **RESOLVED (2026-06-12).** Prod #17 re-fetched KLAC (one Adjustment.ALL pass) → `no_extreme_backfill_jump` re-run is **GREEN (0 unexplained jumps across 785 names)**; I confirmed the denylist-removal condition to prod/exec. **SPLIT AUTO-EXEMPT INVERSION FIXED (2026-06-12):** 449bcf2 auto-EXEMPTED a jump landing on a split ex_date — but under Adjustment.ALL a correctly-adjusted split is SMOOTH, so a >3× jump ON a split date means the adjustment FAILED; auto-exempting it HID exactly the bug the check exists to catch (latent landmine — today it matched nothing, no active false-pass). Now exemption is the **manual allowlist ONLY** (genuine non-split events — the 13 real movers); the #18 corporate_actions table is used purely to **ANNOTATE** a flagged jump ("ON split ex_date → ADJUSTMENT FAILED") for triage, never to suppress. Verified: scoped re-run still PASS (3 manual exemptions, 0 split jumps). NOTE: the table holds **72** split rows (the earlier "216" was the ±1-day expanded tuple count). COMPOSES with prod's current-deflation check (backfill close vs fresh quote ~5%). Standing invariant in the `--full` nightly tier. |
| P1 | tradeagg-not-at-scale | **The "at-scale" trade-agg parity proof is NOT YET REAL (task #15).** Per-minute drill (2026-06-11): the live STREAM captured only **~10 of 50 names** for the entire day until ~15:51 ET, when the subscription scaled 10→50; backfill had all 50. So the headline n_trades-within-2% 98.05% / **sign 99.82%** is essentially a **10-NAME proof + a ~10-minute 50-name window (15:51-16:00)**, not a 50-name full-session proof. Where the stream DID capture a name, parity is excellent — 15:30/15:45/15:55 all 100% count+sign (the overnight/last-cadence anchor is CLEAN; verdict not tainted). Residual threats: (a) **16:00-ET closing-print minute = 14% within-2%** (closing-auction divergence) → exclude ≥16:00; Modeller's ≥15:50 OFI line is safe. (b) **backfill trade-agg is RTH-bounded** (no post-16:00 ET) → post-close OFI has NO backfill to validate against. | **RESOLVED — PROVEN AT SCALE (2026-06-12, the first true full-50 settled-day proof).** prod ran 6/12 backfill-aggs (36,416 rows / 50 syms); I ran scripts/trade_agg_parity_settled.sql on 6/12. COVERAGE is the headline: **stream 50 / backfill 50 EVERY hour 04:00-16:00 ET** (36,334 overlap minutes) — a genuine full-session 50-name proof, NOT the 6/11 10-name proxy. Results vs the ≥98% gate: **n_trades within-2% = 99.79%** (corr 1.0000, mean abs diff 0.11); **tick-rule SIGN agreement = 99.85%** (the hardest threat — holds at scale); signed_vol within-2%-of-vol = 99.41%. By hour: 100% all premarket+RTH; only the 16:00 closing hour dips to 95.5% (known closing-auction divergence) — and the Modeller's ≥15:50 OFI line is CLEAN (section 6: 15:50-15:59 all 100% count / 100% sign). **M2 exit criterion "settled-day I2b ≥98% at scale" = MET. Order-flow data is trustworthy at current scale → green light for the 500-name scaling.** Standing residual (unchanged, by design): backfill trade-agg is RTH-bounded, so post-16:00 OFI has no backfill to validate against — exclude ≥16:00 / keep OFI ≤15:59. |
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

- **2026-06-12 #2 — Post-restart clean-subscription verification (my coverage-question (b)).** Probed
  whether the ingestor restart ACTUALLY swapped to the clean 1000-equity subscription or silently
  kept the contaminated list. First look ALARMING: 1,053 distinct symbols streaming in the last 20m,
  12 known ETFs (SOXL/TQQQ/SQQQ/UVXY/VXX/SPY/QQQ…) among them. Careful timing analysis = CLEAN, with
  a precision nuance: the ingestor restarted 20:02 UTC (16:02 ET, POST-close); the ETF bars' latest ts
  is 16:01 ET (pre-restart) with 0 bars in the last 5m. So the 1,053 = union of pre-restart
  (contaminated) + post-restart bars across the restart boundary, NOT a failed swap. CONFIRMED clean at
  the SUBSCRIBE-CALL level: the restart's startup log subscribed bars for exactly 1000 symbols and I
  scanned the full list — ZERO ETFs (no SOXL/TQQQ/SPY/QQQ/leveraged/inverse). **BUT the precise honest
  state: clean is proven at the subscribe-list level, NOT yet at the bars-ARRIVING level** — the restart
  landed after the close, so no RTH has elapsed to prove clean bars actually flow. First bars-level
  confirmation = MONDAY's open (re-run this probe at ~09:35 ET: 0 ETF symbols in stream bars after the
  open). Logged so the verification isn't assumed-complete.

- **2026-06-13 #3 — Model-server LIVE prediction-serving path consistency (a part I'd never poked).**
  The whole edge thesis routes through `predictions` (model-server writes ranked deciles each
  cadence) yet I'd only ever validated the research panel + execution, never the SCORING BRIDGE.
  Four adversarial checks on lgbm_fwd_30m_v1.0.0 (23,500 rows / 27 ts / 6-10–6-12):
  (1) **rank↔score consistency: CLEAN** — 0 inversions / 23,500 (rank is 0-indexed, score-desc,
  decile 0=top). FIRST cut showed 23,455 "mismatches" — that was MY 1-indexed `row_number()`
  off-by-one, NOT a bug; caught it by inspecting actual rows before reporting (P1 evidence-first).
  (2) **look-ahead: CLEAN** — 0 / 23,500 rows written before their ts; min lag +2m05s (model writes
  a ts-labeled score only AFTER that minute closes), median 141s. PIT-honest at write time.
  (3) **stale-reuse: CLEAN** — only 3.97% of consecutive-ts score pairs byte-identical (coincidental
  ties, not wholesale batch copying).
  (4) **score degeneracy — REFINED a stale ledger claim.** The old `preds-degenerate` P1 says
  "~80% within 1bp of 0 → basket is tie-break noise." TRUE for the panel as a whole (only ~142
  distinct scores / 776 names per batch, ~18%) BUT the degeneracy is confined to the DEAD MIDDLE:
  deciles 2-7 (~460 names) span just -0.000027..-0.000002 (deciles 2/5/6 have exactly ONE distinct
  score — every name tied), while the TRADED EXTREMES are genuinely differentiated — decile 0 (top
  longs) 65% distinct up to +0.0088, decile 9 (top shorts) 48% distinct down to -0.0010 (~100× the
  middle's range). **So the executor's actual basket (top/bottom deciles) is NOT tie-break noise on
  this model — the middle being flat is the correct "no opinion" expression of a no-edge price model.**
  This is a MORE PRECISE and less pessimistic picture than the blanket ledger claim for the *traded*
  names. Net: serving path is internally sound (rank/PIT/no-reuse); degeneracy is real but middle-only.

## Canonical-close residual characterization (2026-06-13, #14 driver — prod owns the fix, I quantify)

Independent scoped re-drill (6/11 settled day, per-symbol, schema `bars_1m` source='stream' vs
'backfill' on (symbol,ts)) confirms the 0.97% parity residual is purely the close-methodology
offset, unchanged: ~10-15 large-caps each mismatch **~100% of their bars by a small CONSISTENT
<1% amount** — SPYM 0.28%, ALB 0.26%, GPN 0.38%, NDAQ 0.36%, WMB 0.74%, AMT 0.96%, CB 0.31%,
ADP 0.76%, BR 0.68%, DKS 0.56% (avg_rel per name, ~100% of bars, same direction every bar). This
is the official/consolidated minute-close vs stream last-trade-minute-close difference, NOT random
tick loss. KLAC still shows its 9.0× split artifact on 6/11 (pre-ex day) — correctly EXCLUDED from
the gate by `_split_cutoffs()` (KLAC ex=6/12). **FORWARD-LOOKING M2 RISK (flagged to prod/modeller):**
these same ~10-15 names will be in the top-500-by-ADV order-flow universe. If trade-agg/OFI uses
last-trade prices at the minute boundary but the panel/labels reference consolidated close, the OFI
sign on exactly these large-caps is computed against an inconsistent close reference — a 25-95bps
per-name systematic basis. prod's #14 canonical-close decision should pick ONE close convention used
identically by trade-agg, bars, and labels, or OFI inherits this basis on the most-liquid names.

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

- **2026-06-13 — M2 sharded ingestion 50→512 (prod commit 306eaba, topology A), reviewer=qa.**
  **APPROVE for the weekend dry-run; 3 conditions before the Monday deploy, none blocking the
  dry-run. The REAL gate is the settled-day #15 re-proof at 512 (my own), NOT this code review.**
  Reviewed adversarially against my 3 mapped gates:
  - **GATE 2 (parity survives 512) — PASS BY CONSTRUCTION, the strongest part.** The aggregation
    is the SAME quantlib `aggregate_trades`/`aggregate_quotes`; the buffer/`flush_through`/
    `flush_minute`/`ON CONFLICT DO NOTHING` logic is BYTE-IDENTICAL to the single-process path
    (verified via `git show 306eaba^:services/ingestor/main.py`). Sharding only changes process
    topology; `shard_for`=md5(symbol)%n (NOT hash(), determinism-safe) gives each symbol exactly
    ONE worker, so per-symbol `tick_state` is never split → the 99.85% tick-rule sign proof
    TRANSFERS. tests/test_sharding.py proves routing parity at every shard count (3 passed, I ran
    them). **NEW failure mode sharding introduces that the 50-name proof can't see: cross-process
    queue ordering — single reader producer + FIFO per-shard queue preserves a symbol's trade/
    quote/bar order, so it SHOULD hold, but only the settled-day #15 re-proof at 512 confirms it.**
    Standing pre-existing residual (NOT a regression, already inside the 99.85%): a tick arriving
    AFTER its minute's bar re-buffers and the re-flushed minute is dropped by ON CONFLICT — same
    in single-process; my proof was measured against it.
  - **GATE 1 (coverage invariant present) — PRESENT & good, with ONE real gap (condition #1).**
    coverage.py ships per-shard Prometheus gauges + a consecutive-silent-RTH-minute alarm — the
    capture-regression signal I asked for, day one. GAP: it detects a subscribed name going
    trade-silent, but is BLIND to a WHOLE-NAME bar dropout / a wedged worker — a worker that stops
    calling record_bar emits nothing, so its gauge goes STALE (looks identical to healthy in
    Prometheus), and a dropped symbol's streak FREEZES instead of incrementing (record_bar early-
    returns on absence). Classic "silence is not success." CONDITION #1: add a per-shard liveness/
    gauge-freshness alert (and an absolute "all N expected names produced a bar this minute" check)
    so a wedged worker / dropped subscription ALARMS instead of flatlining silently.
  - **GATE 3 (subscription semantics) — CLEAN.** OFI set = top-512 by ADV from `universe_membership
    WHERE in_universe` → inherits the equities-only fix; ETFs can't enter the trade/quote tier.
    QQQ/SPY/IWM are bars-ONLY market-context, explicitly out of OFI (so my Monday bars-level probe
    should see EXACTLY those 3 ETFs in stream bars and no SOXL/TQQQ class). CONDITION #2: `OFI_SYMBOLS_SQL`
    filters `adv_dollar IS NOT NULL` with no `len(ofi)>=500` assert — if ADV is NULL for many names
    on a rebuild date the OFI set silently shrinks under the M2 floor. Add a build-time assert.
  - **CONDITION #3 (test completeness, non-blocking):** test_sharding_parity passes each symbol's
    ticks as ONE list — proves ROUTING parity, not the per-minute buffer/flush interleave (where a
    real divergence would live). Covered by transitivity (flush code byte-identical) but the test
    doesn't assert it; add a minute-boundary-interleaved parity case.
  Verdict: code is sound; conditions are hardening, not corrections. **My settled-day #15 re-proof at
  512 ticks the M2 criterion — that's the gate, code review is necessary-not-sufficient.** PROCESS
  NOTE: my ledger commit landed on the prod-architect/m2-sharding branch (shared-tree checkout);
  flagged to Manager to land on master at merge — did NOT rewrite the shared branch myself.
  - **MANAGER RULINGS (2026-06-13, binding):** cond #1 (per-shard liveness/freshness alert) and cond #2
    (OFI≥500 assert) are **DEPLOY-BLOCKING on prod for Monday** (Manager told prod). cond #3 non-blocking.
    **My #15 re-proof at 512 runs TUESDAY** on the settled 6/15 data (prod fetches 6/15 backfill-aggs
    post-close Monday → I re-prove Tuesday; in the Manager's deploy coordination matrix). Ledger commits
    on the sharding branch: LEAVE them (merge carries them). OFI-marginal-IC-over-ret_5m early read: modeller
    running it before Monday, labeled not-a-verdict (3d=noise), real pilot at ~10 full-session days; gates
    nothing. My canonical-close basis warning is now an explicit input to prod's #14 ONE-close-convention
    decision. **STANDING OBLIGATION: Tuesday — re-prove trade_agg_parity ≥98% at 512 on settled 6/15 (the
    M2 exit criterion tick); Monday ~09:35 ET — bars-level clean-subscription probe (expect exactly QQQ/SPY/
    IWM in stream bars, no SOXL/TQQQ class).**

- **2026-06-12 — #19 DEPLOYED & VERIFIED LIVE + after-review of hotfix 899c72c (qa).** #19 is live;
  reconciliation_log now carries the rich detail my notional-neutrality condition required — today's
  16:00 ET rows read `intended 3L/3S, filled 3L/1S, unfilled=[AMPX,FLY], net_notional=+$353.17,
  has_rich_detail=true`. vs my captured BEFORE-baseline (1,557 rows all ok:true, ZERO rich fields) =
  the finding is provably CLOSED. The symmetric reconcile surfaced the exact dollar-skew + 2 unfilled
  shorts the OLD reconcile reported ok:true blind on. NOTE: the row still shows ok=true — CORRECT per
  our #19-Q3 agreement (per-cycle ok trips only on unexpected+rejected, no flap); the HARD incomplete-
  fill/lopsided gate is MY per-day `fill_reconciliation` invariant (build next — must FAIL on today's
  3L/1S + $353 skew). **REGRESSION caught in live-verify (exec fixed, I after-reviewed 899c72c = CORRECT):**
  #19's terminal-status writeback flips orders_log.status 'submitted'→'filled'/'canceled', which broke
  the task-#7 `execution_slippage` view (filtered `status='submitted'` → 0 rows). Fix re-keys it on
  `alpaca_order_id IS NOT NULL` (the fills_log inner join already restricts to filled legs) — correct,
  status-independent. **I grepped the whole codebase for other status='submitted' consumers: NONE broken**
  — only survivors are the UPDATE that SETS it and the `status!='intended'` idempotency guard (still
  valid, terminal states are all != 'intended'). LESSON for the fill_reconciliation invariant + future
  reviews: any consumer keyed on a mutable status enum breaks when a writeback starts populating it;
  prefer keying on stable columns (alpaca_order_id, submitted_at).

- **2026-06-12 (UPDATE) — ex-div verify COMPLETED via modeller-2's committed SQL (adf9415).**
  The earlier caveat (couldn't run the bars-based add-back — OOM) is now CLOSED: modeller-2's Query 2
  filters bars to `time='15:59'` (one bar/symbol-day, few chunks) instead of my `last(close)` agg, so
  it runs. Reproduced Query 2 EXACTLY: mean_label −0.005157, neg_div_yield −0.006103, hygiene-corrected
  **+0.000946, missing_px=0** (every ex-night has a 15:59 close — no price-proxy coverage gap). Closed
  the 3 adversarial angles modeller-2 raised: (1) alignment re-confirmed (label_date+1==ex_date, the
  directional bucket split is the proof). (2) RESIDUAL benign: NOT date-clustering — the 3,291 ex-nights
  spread over 462 dates (avg 7.1/date, busiest = 1.09% of total), so no high-leverage-date artifact; the
  +0.00047 overshoot is consistent with cross-sectional demean + 15:59-vs-official-close proxy.
  (3) COVERAGE clean: action_type='cash_dividend' is PURE (splits 48 + stock_dividends 11 are separate
  types, zero leak); 607/785 panel symbols (77%) pay dividends — broad, not large-cap-skewed, so the
  non-ex baseline is a fair counterfactual. **VERDICT STANDS, now fully reproduced end-to-end. Battery
  interpretation against ex-div-corrected labels is sound.**
  - **OUTCOME (modeller-2, ea2c1eb): corrected battery DONE.** Removing the ex-div artifact LOWERS
    apparent overnight IC on every config (raw 0.0142→0.0096; lambdarank 0.0358→0.0339 — the model was
    partly predicting the mechanical dividend drop) but survivorship-neutralized sharpe stays NEGATIVE
    everywhere (raw −1.79→−2.18). Net: genuine label-hygiene win, NO hidden overnight alpha; Family B
    discarded. My full verify puts that on solid ground.
  - **PENDING TIER-1 REVIEW (routes to qa, label semantics):** modeller-2's production quantlib/labels.py
    ex-div hygiene fix (add yield back on affected nights). Two things baked in from my+Manager notes:
    (a) yield DENOMINATOR = official/adjusted daily close, NOT the 15:59 proxy → kills the +4.8bps
    over-correction I flagged; (b) needs label-VERSIONING (basis/version column, #22's first brick) to
    persist corrected labels WITHOUT overwriting frozen canonical ones. PR gated on #22 landing — review
    it when it arrives; specifically check the denominator change actually neutralizes the over-correction.

- **2026-06-12 — 827f478 (modeller's ex-div overnight-label artifact diagnostic), verified by qa.**
  CONFIRMED — the diagnostic holds; it's a real label-hygiene finding, safe to interpret the
  corrected battery against. Adversarial checks I ran:
  (1) **Reproduced the core buckets EXACTLY** with my own SQL (overnight label by ex-date relation):
      non-ex baseline +0.000474 (n=420,635), ex==label_date +0.000157 (4,098, no effect), **ex==
      label_date+1 (fwd open = ex-morning) −0.005157 (3,291) = the −51.6bps mechanical drop.** Digit-
      for-digit match to their numbers.
  (2) **Ex-date alignment is PIT-correct, NOT off-by-one** — the off-by-one would have FABRICATED the
      whole effect, so this is the make-or-break: the directional SPLIT is the proof. The effect lands
      ONLY in label_date+1==ex_date (forward open is the ex-morning) and is ABSENT in label_date==ex_date
      (forward open already post-ex). If alignment were shifted, the drop would appear in the wrong
      bucket; it doesn't. Overnight label ts = 15:30 ET on close-date D, value = close(D)→open(D+1); the
      drop correctly hits the night whose D+1 morning is ex.
  (3) **No multi-class double-count** — 0 duplicate (symbol, ex_date) cash_dividend rows in
      corporate_actions_pit; the 3,291 affected labels each match exactly one dividend.
  (4) **Magnitude dimensionally confirms yield** — affected-night dividends avg $0.69 / median $0.475;
      −51.6bps implies an avg affected price ~$130, exactly right for mature dividend-payers. Their
      add-back arithmetic is internally consistent (−51.6 drop + 61.0 yield = +9.5 net ≈ baseline).
  CAVEAT: I could NOT re-run the bars-based add-back myself — the prior-close join over the 253M-row
  hypertable × 613 label partitions hit `out of shared memory / max_locks_per_transaction` (same class
  as P2 jump-check-oom; the DB's lock pool can't take these wide multi-partition joins). I verified the
  yield magnitude dimensionally instead, which is sufficient given the exact bucket reproduction. The
  residual after correction is +4.8bps POSITIVE vs baseline (slight OVER-correction), i.e. NO hidden
  negative second mechanism (withholding/borrow drag would leave a residual NEGATIVE drag — it doesn't);
  the ~15% "unexplained" is correction overshoot, consistent with a yield denominator (prior close)
  marginally off, not a second artifact. **VERDICT: hygiene fix justified. Production label fix comes to
  me as the first Tier-1 PR (label semantics = my review map).**

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
