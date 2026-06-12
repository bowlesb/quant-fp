# Experiment & Decision Journal

Append-only. Newest entries at the top. Experiments record: hypothesis, config
hash, out-of-sample result, verdict, next step. Decisions record what changed and
why.

---

- ===== M1 CLEAN v1.1.1 PANEL DONE — exit criteria GREEN (2026-06-12 ~07:05, Prod/Architect) =====
  Task #2 COMPLETE. Clean equities-only panel rebuilt as set_version **v1.1.1** (v1.1.0 features left
  frozen as dirty evidence; labels overwritten since they have no version column). Two script bugs
  hit + fixed first: (1) `docker compose run` STOLE the while-loop's stdin -> only month 1 built;
  fix = bash-array iteration + `</dev/null`. (2) build_labels OOM'd on Postgres locks (612-chunk
  hypertable, full-range executemany > max_locks_per_transaction=64); fix = chunk labels monthly
  (~21 chunks/run; per-ts cross-sectional demean is self-contained, so chunking is correctness-neutral).
  EVIDENCE (all exit criteria green):
  - feature_vectors v1.1.1: **5,525,040 rows / 613 dates / 785 symbols / 2024-01-02..2026-06-11**;
    per-date breadth min 696 / avg 742 / max 784 (matches the clean ~742-name universe).
  - labels: fwd_30m 4,840,765 (613d) / fwd_60m 4,416,876 (613d) / overnight 428,024 (600d).
    Modeller's gate PASSES: min(computed_at) = 06:43Z (today, after the rebuild start) for all 3.
  - **NaN coverage: 0.000% on ALL 21 features** (no silent degradation; momentum warm from the
    2023-12 backfill so even mom_10d is 0% NaN at the 2024-01-02 start).
  - overnight is 600/613 dates: ~13 month-boundary-Friday + end-of-history gaps from monthly
    chunking (next session in the next month not in the +1day fetch). ~2%, secondary horizon —
    noted to Modeller; patchable with overlapping windows if it matters.
  Usable training window = 2024-01-02..2026-06-11 (momentum pre-warm; last-day late-cadence rows
  unlabeled by forward horizon). Handing Modeller the v1.1.1 go-signal -> unblocks battery #4.

- ===== M1 CLEAN-UNIVERSE REBUILD DONE + STALE-IMAGE BUG CAUGHT (2026-06-12 ~04:25, Prod/Architect) =====
  Task #1 COMPLETE. **STALE-IMAGE TRAP (running!=intended), caught before it poisoned everything:**
  the first build-universe-history ran on a backfiller image built 06-11 14:08 PDT — ~6.5h BEFORE the
  is_etf_like fix (814e548 @20:44). `docker compose run <svc>` BAKES source into the image (no volume
  mount), so it ran pre-fix select_universe and RE-CONTAMINATED the universe (~175 ETFs/date still on
  "rebuilt" dates). Caught by verifying already-rebuilt EARLY dates BEFORE trusting completion (they
  still had iShares/SPDR/ProShares). Also found quant-scheduler stale (06-10) — the LIVE daily universe
  builder, which would have re-contaminated universe_membership on its next pre-open run.
  FIX: rebuilt + verified backfiller AND scheduler images (in-image is_etf_like: SPDR/iShares excluded,
  TQQQ dropped despite 9e9 ADV, Apple kept), re-ran.
  **AUTHORITATIVE VERIFICATION (code's own is_etf_like over EVERY member): 614 dates, 455,881 members,
  0 ETF-like violations across 0 dates.** Sizes min 696 / avg 742 / max 1000 (the 1000 is the live 6/12
  scheduler row). Contamination removed: 573,149 -> 455,881 rows (~117k ETF members gone).
  CORRECTION of an earlier claim: clean universe = ~715-742 equities/date, NOT ~885. The "~885 + ~160
  displaced equities" finding was a MISREAD of the stale (ETF-included) run. Reality: contamination was
  purely ADDITIVE ETFs (~210/date); the clean equity set ~= the old equity portion; no meaningful
  displacement. The substantive change for Modeller is LABEL re-demeaning (cross-sectional median no
  longer includes fund returns), not new names.
  DATA-COVERAGE GAP (noted, M2): historical panel is backfill-limited to ~715-742 names (we only
  backfilled the OLD universe's ~1006 symbols); the LIVE clean universe fills to 1000 from all 7439
  tradable equities -> ~285 live names have NO backfilled history. Full clean-universe backfill is M2/
  task-#9 territory.
  Pre-open #6: 6/12 live membership was contaminated (1000, 200 ETFs) and maybe_build_universe SKIPS
  when a row exists -> deleted it + restarted the fixed scheduler -> rebuilt CLEAN (1000, 0 funds).
  model-server ready (lgbm_fwd_30m_v1.0.0 loaded, 30m cadence firing, staleness guard correctly idling
  overnight). NOTE for QA: maybe_build_universe uses datetime.now(UTC).date() not ET — latent calendar bug.
  NEXT: panel rebuild as v1.1.1 (monthly-chunked features + label overwrite), then unblocks Modeller #4.

## 2026-06-10 — Project start

- Decision: build fresh at `~/quant`, ignoring prior repos (Edgar,
  automated-day-trading) per Ben's explicit request.
- Decision: committed approach = cross-sectional short-horizon ML ranking on a
  ~1,000-symbol liquid universe; LightGBM; paper-first with statistical gates.
  Rationale in `ARCHITECTURE.md`.
- Started Phase 0 foundation.
- Tore down legacy Edgar Docker stack (containers/networks removed, data volumes
  preserved) to free the single Alpaca data websocket. Old code/data left on disk.
- Ingestor live on SIP feed; verified bars_1m persistence end-to-end for 10 symbols.
  Confirmed the account already has SIP (Algo Trader Plus) entitlement.
- Executor + reconciliation live; hello-world paper order verified; reconciliation
  caught a stray DLTR paper position from the old system.
- Reset paper account to clean baseline (flattened positions, cancelled orders,
  truncated test order/fill/recon rows). Ben approved.
- Scheduler live computing data_quality_daily coverage; dashboard shows it.
- Phase 0 service set complete: timescaledb, ingestor, executor, scheduler,
  dashboard, prometheus, grafana. Remaining for gate = clean-days accumulation +
  reboot-survival check.
- Built shared `quantlib` aggregation library (parity cornerstone) with a
  live-vs-batch parity test; extended ingestor to trades/quotes via quantlib
  (monorepo build context). Verified rich, sane aggregates landing live.
- Wrote docs/RESEARCH.md: 40-item ML-approaches backlog organized by ring and a
  first experiment wave to exercise the full gauntlet once Phase 2/3 infra exists.
- Freed 2.1TB on SSD (deleted regenerable carved files after proving 15/25
  byte-identical re-extraction from sdb; kept curated extracts + recovery scripts;
  sdb/sda untouched). SSD now 2.6TB free; backfill unblocked.
- Built universe construction (quantlib.universe + scheduler): screened 12,722
  tradable equities, selected exactly 1,000 most-liquid (price>$5, ADV$>$10M;
  cut at ~$161M ADV) into universe_membership for the day. 8 tests pass.
- Built backfiller (REST bars -> source='backfill') + validate-bars gate.
  FINDING (2026-06-10): streamed vs same-day REST bars match 99.76% on OHLC,
  95% incl. volume; all mismatches are tiny late-corrections (volume ±1 print,
  sub-cent closes). Real-time bars are built just before late prints settle, so
  REST (post-consolidation) differs slightly. IMPLICATION: treat source='backfill'
  as authoritative for training/features; source='stream' is what we trade on live.
  This is a real, bounded source of train/serve skew to track — exactly why the
  schema keeps both sources. Official gate number should be measured on a fully
  settled prior day, not same-day.
- Aggregate parity validated on real data: trade_agg 95.2% within 2% (mean rel
  diff 0.7%), quote_agg spread 100% over 63 overlapping minutes.
- Scaled live bar ingestion to the full universe: confirmed 951 distinct symbols
  streaming bars in a 90s window. Trades/quotes kept on the liquid 10 subset.
- Phase 2: built v1 feature engine (quantlib/features, 18 features) + historical
  feature-store builder + live feature-computer, sharing quantlib/featurestore.
  FEATURE replay-equivalence = 100% identical (stream vs historical recompute).
  Feature-level train/serve skew eliminated by construction. Cleanup: removed dead
  services/status scaffold + unused webhook config.
- Phase 3 prep: forward-return cross-sectional labels (quantlib/labels). Built
  universe features (30,970) + labels for today; created training_data view
  (feature_vectors JOIN labels). Panel currently lopsided: broad breadth (998
  symbols) at ~1-2 timestamps + deep time on the 10-symbol subset, because
  full-universe stream bars only span ~31min so far. Real panel needs the 7-day
  backfill built on source='backfill'.
- PRIORITY-E SANITY LOOK (NOT an edge claim): per-feature Pearson corr vs fwd_30m
  on n=2339 rows from ~51 same-day timestamps. Recent-return features correlate
  NEGATIVELY with forward return (ret_15m -0.27, rel_ret_30m -0.23, ret_30m -0.15)
  = short-horizon reversal signature, directionally as hoped. vol_30m +0.36 likely
  a within-cross-section volatility artifact. CAVEAT: single day, overlapping/
  autocorrelated obs, one regime, Pearson not rank-IC — statistically meaningless;
  pipeline-sanity only. Defer real IC to multi-day universe panel from backfill.
- ML side-exploration (subagent, side work — does NOT divert from platform build):
  concrete first-wave plan folded into docs/RESEARCH.md. Key takeaways: best
  cost/latency fits are the OVERNIGHT book and order-flow-CONFIRMED intraday
  continuation; reversal is real but the short book's blowup risk (gate w/ news +
  vol). Two cheapest high-leverage wins: vol-scale the label, add cross-sectional
  signed-volume z-scores + rank transforms. Must-dos: shuffle-label leakage canary,
  embargo ≥ max(label,lookback), session-aware overnight purge, in-fold scaling,
  deflated Sharpe + one-touch lockbox, IC stability > peak IC. Feature tiers:
  Tier1 (cheap, high EV) = signed-vol z 5/15/30m, cross-sectional rank transforms,
  vol-normalized returns, late-day/closing-auction flow, sector-neutral residual.
- STANDING 4-ROLE TEAM established (Ben, 2026-06-10): every wake = Manager(me) +
  parallel QA / Modeller / Production-Engineer specialists on the shared state; manager
  synthesizes + executes. OPERATING_LOOP step 0 rewritten.
- TEAM/QA-TESTER report (synthesis):
  - P0: TODAY's (2026-06-10) feature rows have minute_of_day/day_of_week in UTC (stale
    from a pre-DST-fix build) mixed under the SAME set_version as correct rows, and are
    in training_data. ROOT CAUSE: feature_vectors upsert is ON CONFLICT DO NOTHING, so
    the rebuild couldn't overwrite today's pre-existing stale rows. FIX: rebuilds must
    DELETE-then-insert (or upsert) for a (date,source,version); recompute today.
  - P1: training_data joins on (symbol,ts) only; labels has 2 horizons + no set_version
    -> 662k vectors fan out to 1.09M rows (2x). The TRAINER filters horizon+set_version
    (so training is fine), but the VIEW is a foot-gun — fix it (filter horizon).
  - P2: feature/label builders default bar_source='stream' -> make 'backfill' default
    for historical builds (stream exists only 1 day).
  - P2: build_universe_history uses a HARDCODED UTC window '13:30-20:00' (DST-fragile)
    instead of is_rth() -> biases winter-date universe screening. FIX: use is_rth.
  - P3: micro 99.9% NaN (confirmed); vol_z_30 unwinsorized fat tail (-519..+17524).
  - GOOD: integrity clean; stream-vs-backfill parity excellent (100% close, 99.41% vol);
    latency healthy. Suggested new probes (calendar-ET, view fan-out, compression) — add.

- ===== MANAGER 4-ROLE SYNTHESIS & PRIORITIZED PLAN =====
  Reframe (PE): the OPEN is primarily VALIDATING the live-scoring path, not first live
  trading. ML quality is separate (Ben). So:
  A. OPEN-CRITICAL / SAFETY (do before open): model-server membership date-guard (no
     union fallback) + deterministic tie-break; throttle backfill-manager during RTH;
     EXECUTOR in DRY-RUN (compute+log basket, no submit) reading predictions, excluding
     ETFs + shortable-only shorts + staleness guard. Validates predictions->basket safely.
  B. DATA-INTEGRITY (quick, prevents corruption): rebuild = DELETE-then-insert (QA-P0);
     build_universe_history use is_rth (QA-P2); builders default backfill (QA-P2); fix
     training_data view (QA-P1).
  C. MODELING QUALITY (next cycles, NOT open-blocking): exclude ETFs from universe +
     re-rank; ship 13-feature v1.1.0 (kills micro identity-leak per Modeller+PE+QA);
     recompute today's panel; rank-label E0' baseline; overnight model is the destination.
  D. OPS HYGIENE: fix TimescaleDB compression (0/74 chunks, disk time-bomb); feature_
     vectors retention; register_feature_set hoist + N+1; stale-data auto-halt.
  Executing A (safety) now; B/C/D queued in priority order.
- TEAM/PRODUCTION-ENGINEER report (synthesis):
  - P0: model-server live loop has NEVER run a real cadence in prod (up outside RTH);
    the live source='live' path runs for the first time at the open. Smoke-test it.
  - P0: executor still hello-world; doesn't read predictions (the known last piece).
    PE recommendation: VALIDATE live scoring first, build/trade executor second — don't
    wire an untested signal to orders the same day. (Manager: build executor with a
    DRY-RUN mode first — compute+log basket, no submit — then enable submit once proven.)
  - P0: ship the deferred 13-feature v1.1.0 to kill the micro-NaN identity-leak (live
    universe scoring currently feeds 975 NaN-micro rows into the 18-feat model).
  - P1: **TimescaleDB compression NOT working — 0/74 bars_1m chunks compressed** despite
    policy; DB 6.8GB growing ~185k bars/day. DISK TIME-BOMB; fix before raising backfill
    target. Also no retention on feature_vectors.
  - P1: backfill-manager hammering DB (113% CPU) with repeated near-empty current-month
    passes; THROTTLE/PAUSE during RTH so the open burst isn't starved of I/O.
  - P2: build_feature_store calls register_feature_set 992× in the loop + N+1 queries —
    hoist register out; batch bar loads. model-server load_membership keys on today's
    UTC date -> if scheduler hasn't written today's universe pre-open, falls back to the
    51-date UNION (wrong universe) — ensure scheduler writes today's membership pre-open.
  - P2: no stale-data auto-halt (ARCHITECTURE rule #6) — implement before any real basket.
  - GOOD: real-time/backfill parity is genuinely shared code (the system's best property).
- TEAM/MODELLER report (synthesis):
  - Panel shape: FAT cross-section (~928 names × 612 on-cadence ts = 568k rows) but
    THIN time (51 days). Drives everything: sign-consistency across folds is the only
    trustworthy stat at this depth; no Sharpe claim until ~250+ days.
  - Most valuable next: E0′ = RANK label (within-ts rank of fwd_30m excess) +
    LightGBM lambdarank grouped by ts + DROP micro -> 13-feature v1.1.0. Rank aligns
    with how we trade (deciles), denominator-free, fat-tail-robust. Vol-scaling = E1
    challenger.
  - Micro = SYMBOL-IDENTITY LEAK (non-NaN perfectly identifies the ~10 streamed names;
    the within-ts label shuffle canary does NOT catch a feature encoding identity).
    Confirms drop-to-13.
  - NW-lag check: lag must be label overlap in CADENCE STEPS. Our trainer already uses
    lag=max(1,horizon//cadence)=1 for fwd_30m@30min (adjacent on-cadence labels don't
    overlap) -> t=2.98 is honest. Good.
  - Decision tree E0′→E1(label bake-off)→E2(daily momentum + reversal features, no new
    infra)→E3(OVERNIGHT model = the real destination; amortizes spread vs our latency)
    →E4(micro as liquid-subset enrichment ONLY if E2/E3 plateau; gates universe-wide
    micro infra). Gate-zero every run on canary<~0.003.
  - Trading shape: commit fwd_30m FIRST (validated-clean, exercises the loop) but
    overnight is the destination. LONG-emphasis, short-as-overlay (shorts carry the
    structural foot-guns + blowup risk; restrict to liquid ETB >=$5; report long/short
    IC SEPARATELY). Order types: day marketable-limit + cls/LOC flatten; whole shares.
    BRACKETS = disconnect safety net ONLY, never the exit rule (RTH-only, kills overnight,
    both legs can fire -> pollutes attribution).
  - Requirements: store daily 1/3/5/10d momentum (E2) + late-session/close-structure
    (E3) features via the SAME featurestore path (parity); realized-vol column if E1
    picks vol-scaling. 13-feat v1.1.0 = stable PREFIX subset; don't bump live constant;
    new feats -> v1.2.0+. QA must: 100% replay-equiv for every production feature;
    settled-day bar-parity gate; PIT-universe confirmed; lookahead audit on new feats.
  - Honest gate ladder: harness-trust -> rebuilt-panel -> clean-baseline(E0′) ->
    robustness(no feat>40%, beat ElasticNet, lookahead) -> AFTER-COST(+50% spread,
    ±1-bar; the real kill-gate) -> significance(NW t>=4 AND deflated Sharpe) ->
    time-depth(~250d) -> lockbox(one touch) -> frozen paper campaign. Expects 30m to
    survive IC gates but DIE after-cost -> overnight is the structural answer.
- EXECUTOR RED-TEAM (verdict ADJUST) — P0 CATCH that reprioritizes:
  - **Universe is polluted with leveraged/inverse ETFs/ETNs.** Top model "longs" =
    SOXL(3X)/KORU(3X)/TECL(3X)/IRE(2X)…; "shorts" = SQQQ/VXX/UVXY/SOXS. The screen
    (price>$5, ADV$>$10M) doesn't exclude ETFs, and leveraged products dominate both
    tails of a momentum-ish model. First basket would trade 3X ETFs/VIX ETNs — invalid
    for single-stock cross-sectional L/S AND for plumbing validation. FIX (critical
    path, BEFORE executor): exclude ETF/ETN/leveraged products from the universe (and
    the executor candidate pool); re-rank. ~55 such names in today's universe.
  - Shortability: filter-then-SELECT shorts (walk up from rank N to K ETB single-names),
    not select-then-drop (which unbalances the short leg). 17/39 top/bottom-20 not ETB.
  - Caps/kill-switch must bind BEFORE submit from a FRESH broker snapshot (/account +
    /positions + open-order BP reserve); persist a kill flag that survives restart.
  - Idempotency: INSERT intent (pending) BEFORE submit, UPDATE with alpaca id after
    (current code submits then inserts — a crash desyncs DB vs broker). Deterministic
    client_order_id (rebalance_date,symbol,side,leg,attempt).
  - Day-1 = NO-FLIP, flat-start basket (sidesteps no-long+short-same-symbol + wash-trade
    + flip-sequencing). REST polling is enough to reconcile a tiny flat-start run;
    trade_updates stream NOT required day-1; brackets/cancel-replace/ext-hours NOT day-1.
  - Staleness guard: reject predictions older than ~1 cadence (only stale 19:00 preds now).
  - model-server: argsort is unstable on tie clusters -> add deterministic secondary
    sort (symbol); executor skips rebalance if scores degenerate (std~0 / all-NaN row).
- MANAGER assessment: E2E deploy done; executor is the last piece (~13h to open). On
  track, but the ETF/universe-quality issue means UNIVERSE DEFINITION needs hardening
  (we were under-attending universe quality). Priorities: (1) exclude ETFs + re-rank,
  (2) build executor w/ the safeguards above (no-flip day-1), (3) monitoring panels.
  Synthesize QA/Modeller/Prod reports (running) before the big build.
- E2E TRAIN STEP WORKS (2026-06-10): panel rebuilt over 51 dates (662,954 feature
  vectors, PIT, 30-min cadence) + labels (570,481 fwd_30m). First LightGBM trained
  through the leakage-checked harness: panel 570,481 rows/18 feats/661 timestamps;
  REAL mean rank-IC=0.0205, NW t=2.98 (535 test ts); CANARY (within-group shuffle)
  rank-IC=0.0022 ≈ 0. Model saved to ./models/model_fwd_30m.txt.
  HONEST READ — NOT AN EDGE CLAIM: t=2.98 < our t≥4 gate; thin 51-day panel; residual
  survivorship; NO cost model; settled-day parity gate still open; no multiple-testing
  deflation. The VALUE here is that the canary is clean while real IC is positive →
  the pipeline (backfill→panel→train→IC) is sound and not leaking. This is the E2E
  "train" step working, to be trusted as plumbing, not alpha. NEXT: model-server
  (score live → predictions) + executor (predictions → trivial L/S paper basket).
- E2E REFRAME (Ben, 2026-06-10): with real-time/backfill parity proven, prioritize the
  THIN END-TO-END vertical slice — backfill→train→deploy→paper-trade→reconcile — to SEE
  it run, even trivially (plumbing validation, NOT edge). Strategy reaffirmed: cross-
  sectional ML ranking (long top decile / short bottom decile), LightGBM. ACTIONS this
  cycle: wired per-date point-in-time membership into build_feature_store + build_labels
  (emit feature rows only for that date's members; demean labels within each date's
  cross-section — completes the survivorship fix); added a rebalance CADENCE
  (FEATURE_CADENCE_MIN, ET-clock) so the panel is ~640k rows at 30-min cadence instead
  of ~19M at every minute. Kicked off the full panel rebuild (PIT, backfill bars,
  30-min cadence, all 51 dates) as a background job. NEXT: rebuild labels, train a first
  LightGBM, model-server -> live predictions, executor -> trivial L/S paper basket.
  (Keeping 18-feature v1.0.0 for the first E2E; 13-feature v1.1.0 refinement noted.)
- PROACTIVITY CORRECTION (Ben, 2026-06-10): I tunneled on the data/modeling pipeline
  and neglected EXECUTION. Market-close at 20:00 UTC observed + verified (332 syms
  post-close = extended-hours stragglers, not a fault). Ben: be proactive about
  neglected high-value tracks (esp. overnight); dig into the Alpaca API; stress-test
  it; start trivial paper trades now. ACTIONS: OPERATING_LOOP + memory updated with a
  proactivity/parallel-workstreams directive + overnight menu + EXECUTION as a
  first-class track. Launched a deep Alpaca-execution research agent. Hands-on paper
  exploration (see docs/EXECUTION.md): 4x margin, shorting enabled, market orders QUEUE
  (ACCEPTED) when closed (foot-gun), limit ext-hours rests as NEW, cancel_orders clean.
- CRITIC #5 (pipeline track — logged for when I do the panel rebuild, did NOT act this
  cycle since pivoting to execution):
  - BLOCKER-1: `labels` has no set_version and `training_data` joins on (symbol,ts)
    only — coexisting v1.0.0/v1.1.0 will cross-contaminate; the LightGBM runner MUST
    pin set_version, and the rebuild must RECOMPUTE labels (don't reuse 1-date labels);
    label VALUE depends on which universe demeaned it, which the schema can't encode.
  - BLOCKER-2: build_labels/build_features still demean over a static set — wire the
    per-date outer loop (members per date; feature rows only for members; demean each
    ts within that date's members). Avoid O(dates×symbols×bars).
  - BLOCKER-3: Newey-West `lag` must equal the label overlap in TIMESTAMPS (e.g. 30
    for fwd_30m on 1-min grid); thin ~50-date panel => canary/t are pipeline checks,
    not validation. Verify 30m label near 15:30 ET resolves within-session or NaN.
  - NaN policy: let LightGBM handle native NaN; do NOT fill (NaN density vs time-of-day
    could fake edge); shuffle canary is the arbiter.
  - Don't bump the FEATURE_SET_VERSION module constant until the per-date rebuild is
    proven (it would flip the live feature-computer to 13-vectors and reset replay-
    equivalence to 0 overlap); make the 13-feature vector a stable subset/prefix order.
- CRITIC #4 (wake red-team) — verdict ON-TRACK; findings + actions:
  - [SEQUENCING] Build the HARNESS FIRST on synthetic fixtures (zero dep on the real
    panel; reveals the panel's required shape; another panel pass first = polishing
    trap). DONE: quantlib/backtest.py — walk_forward_folds (purge by label horizon in
    market time), per_timestamp_ic (within-cross-section Spearman, averaged — never
    pooled), shuffle_within_groups canary, newey_west_tstat (deflates overlapping-
    label autocorrelation). 6 trap-fixture tests (leaky-straddle purge, cross-ts-only
    IC~0, real-signal IC, within-group canary kills IC, NW). 26 tests pass. Model is
    pluggable (stub in tests; LightGBM later).
  - [HIGH] Panel has only 1 date despite 51 universe dates — the rebuild must LOOP all
    51 dates (1->~50); that depth is what unblocks Phase 3, not better demeaning alone.
  - [13 vs 18] DECIDED 13: micro is 99.7% NaN universe-wide; LightGBM could learn the
    NaN-pattern as a symbol-identity proxy = leakage into a cross-sectional ranker.
    Drop micro for the universe model; version as v1.1.0 (new feature_sets row); keep
    micro as a liquid-subset enrichment. Implement at panel rebuild.
  - [RESIDUAL RISK, logged honestly — NOT closed] build_universe_history screens only
    symbols present in today's backfill, so truly-delisted names are still absent =
    residual survivorship (smaller than the original bug; acceptable for 90d but real).
  - [OPEN GATE, keep visible] Phase-1 streamed-vs-REST >=99.9% parity gate has never
    formally passed on a settled day (only 1 stream day; 99.76% is same-day/suggestive).
    Nightly auto-validate not yet wired. Don't let Phase 3 momentum bury this.
- CRITIC #3 (wake red-team) — verdict ADJUST; findings + actions:
  - [HIGH] Breadth premise stale: breadth is now UNIFORM (989-1000/weekday back to
    Mar 9; only Saturdays are junk). So the "breadth gate" is trivial (weekday +
    min-symbol), not a subsystem. Confirmed by probe; keeping it minimal.
  - [HIGH] PIT ADV must not use the live Alpaca-defaulted (no end=) fetch = lookahead.
    My build_universe_history reads LOCAL bars_1m with a strictly-prior session window
    -> no lookahead (avoided the trap). Built 51 PIT dates (Mar 30+); membership varies
    by date (earliest 974, latest 992, ~20 differ) = survivorship fix working.
  - [HIGH] Regression it caught in my just-shipped refactor: minute_of_day/day_of_week
    were raw UTC while is_rth was tz-aware -> 60-unit jump at the DST boundary. FIXED:
    both now ET-local (astimezone NY); added a DST-consistency test. 20 tests pass.
  - [MED] Micro features (5 of 18) are ~98% NaN on the full universe (collected only
    for the 10-symbol subset). DECISION (explicit, open for harness cycle): either run
    the full-universe model on the 13 non-micro features, or keep 18 and let LightGBM's
    native NaN handling split on them; lean toward 13-for-universe + micro as a
    liquid-subset enrichment. Revisit when wiring the panel/harness.
  - [MED] Nudge: don't polish the panel indefinitely; the harness (on synthetic
    fixtures + shuffle canary) is the durable value and is buildable now. ACCEPTED:
    after per-date-demean wiring, build the harness skeleton next.
- CRITIC #2 (wake red-team) — verdict ON-TRACK; findings + actions:
  - [HIGH] Backfill breadth NON-UNIFORM (Mar ~989/day, May ~494, Jun ~830) because
    backfill is mid-fill. Rebuilding labels now would demean over a half-universe =
    fresh bias. ACTION: do NOT rebuild the trainable panel until backfill is complete
    AND per-date breadth is uniform; add a breadth gate (skip/flag under-covered dates).
    Also: the "near-empty days" (Mar7/21, May16) are SATURDAYS with stray extended-
    hours bars — RTH filtering drops them.
  - [HIGH] Both bugs confirmed (universe_membership has only 1 date; session_open off
    premarket). Fixes correct.
  - [MED] RTH must apply to LABELS too, and feature lookups must be TIMESTAMP-based
    (positional crosses sessions). ACCEPTED + DONE this cycle (see below).
  - Loop overhead acceptable; critic paying off (found these). Don't let critic+probes
    be the only thing a cheap wake does.
  ACTIONS TAKEN: (1) is_rth() — DST-correct (America/New_York), tested; (2) featurestore
  filters bars+market to RTH, session_open = first RTH bar; (3) features.py rewritten
  to TIMESTAMP-based gap/session-safe lookups (ret/vol/volume-z), tested incl. a gap
  case; (4) label builder filters price series to RTH (forward returns RTH-to-RTH,
  never crossing the session). 19 tests pass. STILL PENDING (panel rebuild, gated on
  backfill-complete + uniform breadth): per-date point-in-time universe + breadth gate.
  Note: existing feature_vectors are now mixed old(positional)/new(timestamp); will be
  fully recomputed in the panel rebuild.
- DATA PROBE BATTERY (now scripts/data_probes.sql; run + extend every cycle):
  Cycle-2 new angles: ingestion latency ~1-4s after bar close (beats <5s target);
  flat-bar rate 7.84% RTH (acceptable tail); RTH bars/symbol-day avg 367/390 with a
  thin tail (min 2 = partial-listing/thin names). Gap-spanning extremes 139->44 as
  backfill fills.
  - Integrity: 0 violations across 11 invariants (OHLC ordering, vwap range, signs,
    grid, imbalance bounds). Clean.
  - Independent cross-check WIN: our trade_agg.n_trades vs bars.trade_count correlate
    0.9982 (98.4% within 5%) — validates the tick aggregation from an independent source.
  - Extreme 1-min returns (139 >50%, max 785%): ALL gap-spanning (prev_ts Mar 31 →
    ts Jun 3). Benign artifact of the temporary April/May backfill hole; self-resolves
    as the manager fills months. Not bad data.
  - REAL FINDING — extended hours + session_open bug: 19.5% of bars are outside RTH
    (13:30-20:00 UTC); earliest bar/day is ~00:00/09:00 UTC. So the feature builder's
    session_open (= first calendar-day bar) is a premarket/overnight price, NOT the
    09:30 ET open → gap_from_open (and RTH assumptions) are WRONG. FIX in the panel
    rebuild: restrict features/labels to RTH and define session_open as first RTH bar
    (also consider timestamp-based feature lookups vs positional, which assume
    contiguous minutes). Added to top priorities.
  - Panel is 94-99% NaN for key features (data thinness + micro only on 10-symbol
    subset) — confirms unfit for modeling; reinforces "read nothing off it yet."
- CRITIC AGENT (wake red-team) findings + my decisions:
  - #1 (ACCEPTED, top priority): survivorship/point-in-time-universe leakage — the
    historical feature AND label builders use universe of max(trade_date) applied to
    all dates, violating ARCHITECTURE rule #4 and biasing the cross-sectional label
    median over survivors. FIX (ahead of the modeling harness): (a) construct
    universe_membership per historical trade_date from backfilled bars via
    quantlib.universe; (b) make build_feature_store/build_labels select per-date
    membership and demean within that date's universe; (c) rebuild the panel.
  - #2 (ACCEPTED but DEFERRED w/ correction): close the Phase-1 parity gate on a
    SETTLED day. Correction to the critic: we only have 1 day of *stream* data
    (started today), so there is no settled stream-vs-backfill overlap yet; earliest
    possible is tomorrow once today settles. Plan: automate a nightly validate-bars
    on the prior settled day in the scheduler. Until then the gate stays honestly open.
  - #3 (ACCEPTED): don't read any edge number off the lopsided same-day panel; gate
    modeling-harness "doneness" on synthetic fixtures + the shuffle-label canary, not
    a live number.
  - #4 (FIXED now): aggregate backfill used DO NOTHING while bars used DO UPDATE;
    made trade_agg/quote_agg backfill upsert too, so re-fetch self-corrects.
- Health-check note: the "symbols streaming in last 90s" probe can transiently read
  0 in the gap between minute close and bar delivery (~up to 60s); use a 2-3min
  window. Verified ingestor healthy (latest bar always ~1min old).
- Info-gathering (priority E): added asset_metadata (Alpaca: exchange + shortable/
  easy-to-borrow/fractionable), refreshed daily by scheduler (13,852 symbols).
  Finding: of the 1,000-symbol universe, 939 are shortable/easy-to-borrow, 983
  fractionable. IMPLICATION: the short leg must be restricted to shortable names
  (~61 excluded) — wire this into portfolio construction in Phase 4.

- ===== TEAM CYCLE (Modeller + Production-Engineer/Architect + QA, 2026-06-10 evening) =====
  E2E LOOP CLOSED (dry-run): executor reads predictions -> valid no-flip L/S basket
  (single stocks, ETF-excluded, shortable shorts) logged not submitted. Staleness guard
  works. Also fixed: TimescaleDB compression (68/74 chunks; DB 6.8GB->2.7GB), added
  model-server STALE-DATA halt + executor SCORE-DEGENERACY guard.
  PRODUCTION ENGINEER/ARCHITECT:
   - Live scoring works at open (5.2s/977 symbols, well within cadence); membership guard
     healthy. CAVEAT: first 1-2 cadences (9:30/10:00 ET) have NaN 60m features -> off-
     distribution; don't trust early prints. Biggest lights-on risk = no stale-data halt
     (ADDED). Compression was just un-run policy (FIXED).
   - Tech debt: backfill-manager re-fetches the WHOLE current month every ~4min (wasteful,
     DB-churn) -> make incremental [last_seen,now]; experimenter writes root-owned host
     files -> add user:uid; batch the ~4k round-trips in build_feature_store later.
   - ARCHITECT DECISION (record): commit to a SHARDED trade/quote ingestion tier (N shard
     processes, same quantlib code for parity) BEFORE the 6yr backfill + order-flow
     features. The modeling roadmap is BLOCKED on universe-wide microstructure (micro 98%
     NaN because trades/quotes only stream for 10 symbols). Keep raw ticks short-retention;
     keep 6yr of AGGREGATES (compressed), not raw ticks. Mind the single-Alpaca-socket
     constraint. Do NOT refactor the live-builder pattern (it's the parity crown jewel).
  QA (new findings):
   - P0: feature-computer wrote UTC-calendar 'stream' rows (stale pre-DST code, 81%); and
     today's 'historical' panel has 4,477 UTC-contaminated rows reaching training_data
     (insert-not-replace). FIX: rebuild = DELETE-then-insert; purge contaminated stream/
     today-historical rows; add a serving-path ET assertion.
   - P1: PREDICTIONS ARE SCORE-DEGENERATE (80% within 1bp of 0; 243 symbols share one
     score) -> deciles decided by alphabetical tie-break, not signal. Consistent with the
     calendar-artifact (~no real signal). Executor degeneracy guard ADDED; current preds
     non-tradeable.
   - P2: 14 non-member rows (leveraged/derived tickers) leak into training_data -> hard-
     filter per-date members + exclude derived tickers.
   - CLEARED: day_of_week ET-correct; per-ts demean exact (median 0); no Inf; view not key-dup.
  MODELLER: price-only features have ~0 cross-sectional signal (honest baseline IC~0). The
   one swing = cross-sectional DAILY MOMENTUM (mom_1/3/5/10d + _rel vs SPY = 8 feats,
   computable from stored bars, 22 pre-panel warmup days available) -> store via shared
   featurestore (parity), bump v1.1.0. research.py needs lambdarank + vol_scaled label
   paths (queue keys set_version/model/device not yet read). Keep calendar only as regime
   conditioners (default experiments to nocalendar). Binding constraint = 51 days, not features.
  MANAGER NEXT (priority): (data) DELETE-then-insert rebuild + recompute today + purge UTC
   stream rows + PIT member/derived-ticker filter; (modeling) momentum features v1.1.0 +
   research.py lambdarank/vol_scaled; (ops) backfill incremental current-month + experimenter
   uid; (architect) design the sharded trade/quote ingestion tier. Open = VALIDATE scoring,
   executor stays DRY-RUN. Honesty: predictions are not tradeable signal yet.

- ===== OWNER-AUDIT TRIAGE (Manager, 2026-06-11) — validation of the owner-framing =====
  An open-ended owner-charter agent (no checklist from me) surfaced 6 real, mostly-
  unflagged issues. The framing WORKS. Triaged + assigned:
  1. [Prod] PHANTOM BACKFILL — the deep backfill never ran: backfill-manager was still at
     TARGET_DAYS=90 (compose said 900, never restarted) + my one-shot committed 0 bars in
     16 min (full-range chunking). The exact "running != intended" bug, recurred in a day.
     ACTION: killed one-shot; restarted manager (now 900); launched reliable month-by-month
     deep backfill. DURABLE GUARD (owed): startup TARGET_DAYS log stamp + a probe asserting
     min(bars.ts) <= today - TARGET_DAYS*0.9. "Running==intended" needs a TEST, not a habit.
  2. [Modeller] **NO COST MODEL anywhere** — the whole signal hunt optimizes rank-IC with
     no net-of-cost P&L. At 30-min cadence (~13x/day) vs ~4bps round-trip spread, even real
     momentum (IC 0.006) is plausibly NET-NEGATIVE. THE #1 STRATEGIC GAP. ACTION: add net-
     of-cost backtest to quantlib/backtest.py (dollar-neutral basket, charge spread/2+
     slippage+borrow, report after-cost Sharpe + breakeven IC); make "beats breakeven cost"
     the FIRST gate. Likely conclusion: lengthen horizon (cut turnover) > any new feature.
  3. [Prod] LIVE COVERAGE MONITORS 10/998 SYMBOLS (1%) — data_quality_daily tracks 10; a
     silent partial-ingestion failure of the other 988 trips no alarm. ACTION: coverage over
     the FULL universe + alert when streamed < 95% of universe.
  4. [Modeller/QA] METHODOLOGY — 51 days x 13 intraday cadences are pseudo-replicated;
     effective N ~ day count (~40), so NW-t over "510 timestamps" is inflated; canary ~= real
     IC. ACTION: compute significance on DAILY-block IC (or block-bootstrap by day); make the
     canary band an explicit numeric gate (|IC| > 2x canary-std).
  5. [Modeller/Prod] NO LIVE TRACK RECORD — predictions has 1 cadence; nothing accumulates a
     live prediction->realized-return ledger. ACTION: nightly live_ic_daily job. THAT series
     (not backtest IC) is what eventually justifies DRY_RUN=false.
  6. [Execution/Risk] executor tracks no position state between rebalances ("no-flip" only
     holds on a flat book); latent (submit off). Close before live + build the kill-switch-
     from-fresh-broker-truth path (currently a scaffold).
  MANAGER CALL: the #1 next build is the NET-OF-COST GATE (#2) — it may reveal we're hunting
  a number that's economically negative, and that lengthening the horizon beats any feature.

- ===== MANAGER SYNTHESIS — 5-role team cycle (2026-06-11, overnight + execution) =====
  Both specialists thought like owners and found real things (the framing works).
  MODELLER (overnight design): the 0.094 IC is NOT trusted — it's contaminated by
  (a) Adjustment.ALL dividend look-ahead (retroactive div adjustment bakes future info
  into the overnight gap; cancels intraday, NOT overnight), (b) earnings-gap dominance
  (un-tradeable event noise, no earnings calendar in DB), (c) survivorship (delistings
  happen overnight; deep panel lacks delisted names). The negative L/S P&L = fat-tail gaps.
  #1 ACTION (Modeller's call, I agree): REBUILD features+labels over the full 323-day deep
  history (still only 51 days built) — attacks BOTH binding constraints at once (time depth
  51->323 AND overnight turnover). Don't run more thin-panel experiments (canary≈IC = noise).
  Design fixes: anchor prediction at ~15:55 close (not 15:30); purge by TRADING-DAY index
  (minute purge under-purges weekends); NW lag=1 (non-overlapping); periods_per_year=252;
  MOC/MOO cost still ~2-3bps one-way (overnight = fewer trades, not cheaper trades); borrow
  on calendar-nights.
  EXECUTION/RISK (cycle 1): LIVE REGRESSION — the reconciliation loop was dropped in the
  f4ed85d rewrite (mine); reconciliation_log 6h stale; the docstring LIED (claimed recon +
  kill-switch scaffold). Fixed the docstring this cycle. No kill-switch/caps-from-broker
  exist (inert in dry-run, fine). ETB (not just shortable) must gate shorts; no marketable-
  limit pricing; no EOD LOC flatten; staleness=35min wrong for overnight. Overnight execution
  (MOC/MOO, ext-hours, gap, borrow) is UNOWNED and changes routing/risk/cost.
  MANAGER COVERAGE-QUESTION ANSWERS (own-the-outcome assignments):
   - Corporate-actions/dividend adjustment -> NEW data-integrity workstream: Prod gets an
     FMP dividends/splits table; QA validates; decide split-only vs ex-div-exclusion for
     overnight labels. (Gates a trustworthy overnight number.)
   - Earnings calendar (FMP) -> Prod pulls; Modeller excludes reporting names. Prereq for
     overnight, not nice-to-have.
   - Deep-panel rebuild + PIT membership over ALL 323 days -> Prod owns (after backfill
     completes); residual survivorship = documented known caveat.
   - MOC/MOO execution feasibility on Alpaca paper -> Execution/Risk validates BEFORE we
     invest more overnight modeling cycles.
   - Recon-loop re-add (read-only, dry-run), ETB gate, fills_log+pnl_daily truth ledger,
     removed-Alpaca-fields audit, docstring-vs-code -> all Execution/Risk (confirmed owned).
  NEXT (gated on deep backfill finishing): rebuild universe-history + features + labels over
  323 days WITH dividend/earnings handling, then re-run overnight under the cost gate on real
  depth (lag=1, 252/yr). STOP running 51-day experiments. Honesty: nothing is edge until it
  clears breakeven net P&L on the deep panel with gaps/divs handled.

- ===== QA DEEP-PANEL AUDIT — rebuild plan REVISED (2026-06-11) =====
  QA (owner) found that running the deep rebuild TODAY would bake in FOUR P0s. Plan revised
  (do NOT naive-rebuild on backfill completion):
  P0s to resolve BEFORE the rebuild:
   1. PIT universe covers only 52 of 443 dates -> load_membership empty for ~270 dates.
      MUST re-run build_universe_history over full depth first. (Also FIXED its DST-fragile
      UTC window -> America/New_York, was corrupting winter-date screening.)
   2. 11-month bar HOLE (2025-03-31..2026-03-02) + ragged breadth (1-symbol days exist).
      Don't rebuild over a void. ADDED breadth floor: cross_sectional_excess returns NaN
      below MIN_CROSS_SECTION=20 (a 1-symbol day's median==itself==>excess 0 = poison) +
      a breadth probe in data_probes. Wait for the hole to fill.
   3. Overnight DIVIDEND LOOK-AHEAD verified: Adjustment.ALL retro-marks pre-ex closes; the
      overnight gap spans the adjustment boundary so the factor does NOT cancel (intraday it
      does) -> ~+div/price bias on ex-div mornings (~3,900 contaminated cells, fat-tailed).
      The 0.094 IC was partly fitting this deterministic, un-tradeable artifact. FIX: SPLIT-
      only-adjusted bars for the overnight LABEL (keep ALL-adjusted for intraday features) +
      ex-div/earnings exclusion (FMP).
   4. SURVIVORSHIP is structural+total: the deep backfill used TODAY's universe (survivors);
      delisted names have NO bars and can never enter. Worst for overnight (delistings/M&A
      gap overnight). MANAGER DECISION: document loudly as an upward bias + do NOT trust the
      overnight tail P&L; defer the delisted-name historical backfill (big, needs historical
      asset list) — accept-and-disclose for now.
  Plus: live(stream)=RAW vs backfill=ALL-adjusted -> same-minute close diverges after any
  corp action (the "100% replay-equivalence" held same-day only) = latent train/serve skew;
  re-fetch under retro-adjustment desyncs panel vs bars (stamp a build epoch, rebuild in one
  pass); warmup NaN-degrade at the gap's far edge (QA-I4 warmup assert still open).
  REVISED REBUILD SEQUENCE: (1) finish gap fill -> (2) build-universe-history over 323 days
  (DST-fixed) -> (3) split adjustment path (ALL features / SPLIT-only overnight labels) +
  ex-div/earnings exclusion -> (4) breadth-floor[done] + warmup assert + breadth probe[done]
  -> (5) THEN rebuild DELETE-then-insert in one pass, stamp epoch.
  COVERAGE ANSWERS: delisted-name backfill = deferred, accept-and-disclose (Prod owns the
  eventual fetch); split-only storage = Prod owns (recommend a SPLIT-only DAILY-bar fetch for
  labels, lighter than dual minute series); adjustment-parity gate = QA on a settled ex-div
  day; breadth guard = DONE (QA owns the probe).

- ===== DEEP PANEL REBUILD UNDERWAY (2026-06-11 00:25 PDT) =====
  Deep backfill complete (668 dates, 2023-12..2026-06, clean breadth). PIT universe rebuilt
  over the full range: universe_membership 52 -> 613 dates (2024-01-02..2026-06-11, avg 933
  members/day; DST-fixed screen). Panel rebuild launched (be93qbgjo, ~3-5h): DELETEd old
  v1.1.0 historical (632,978) + overnight labels (49,225); rebuilding DELETE-then-insert over
  BACKFILL_START=2023-12-01 -> build-features v1.1.0 + build-labels(fwd) + build-overnight-
  labels(SPLIT-only). This is the first ~600-day clean panel (≈12x the 51-day; ~300 effective
  daily samples vs ~40 -> the canary≈IC noise problem should resolve). ON COMPLETION: purge the
  contaminated overnight experiment from results.jsonl + re-run OVERNIGHT under the cost gate
  (NET P&L, NW lag=1) + a deep INTRADAY baseline; judge on net P&L not IC; DISCLOSE survivorship
  (delisted absent) + earnings-gap noise (FMP deferred). Tech-debt noted: build_universe_history
  uses per-row inserts (slow, ~20min/600 dates) -> batch later. Market-day plan unchanged
  (validation+data, DRY_RUN true); open 06:30 PDT, rebuild runs into it (independent of live scoring).

- ===== TWO FAILURES (2026-06-11 ~07:20 PDT, Ben caught the silence) =====
  (1) I went SILENT ~7h: relied solely on the deep-rebuild completion notification and did
  NOT arm a fallback wakeup, so when the job ran pathologically long I never woke — through
  the open I'd promised to watch. LESSON (durable): ALWAYS arm a fallback ScheduleWakeup even
  when notification-driven (per the guidance: long fallback so the loop survives a hang).
  (2) The single-pass deep rebuild was O(n²) in per-symbol bars (build_feature_store rebuilds
  the price dict per cadence point over the growing bar list) -> ~126/1000 symbols in 7h,
  infeasible. FIX: killed it; rebuilding in MONTHLY CHUNKS (first month built in ~2min ->
  full ~60-90min). Proper O(n²) fix logged as TECH_DEBT P1.
  SILVER LINING — the live E2E loop WORKED at the open AUTONOMOUSLY: model-server fired its
  first real cadences (988 syms @13:30 UTC open, 981 @14:00), stale guard held overnight then
  released, dry-run executor logged valid baskets (ETF-excluded single names, not submitted).
  The market-day VALIDATION objective passed on its own while I was silent.

- ===== FIRST LIVE PAPER BETS (2026-06-11 ~07:38 PDT) — submit works, partial fill exposes a real bug =====
  Flipped executor DRY_RUN=false (Exec/Risk GO-WITH-FIXES applied). First live paper basket
  SUBMITTED to Alpaca: 6 orders w/ alpaca ids. RESULT: 3 SHORTS FILLED (HUM/MRVL/PRIM real
  positions), 3 LONG buy-limits RESTING UNFILLED (status NEW). ROOT CAUSE: marketable-limit
  priced off the stale BAR CLOSE ±0.3%, not the live NBBO — market ticked up so the buy limits
  sat below the ask and didn't cross (sells ×0.997 crossed the bid, filled). => lopsided tiny
  net-short book (~$820). This is exactly the kind of real execution behavior dry-run can't show.
  TERMINATION still guaranteed: EOD flatten (~15:48 ET) closes shorts + cancels resting buys.
  EXEC/RISK FIXES QUEUED (P1, for next rep): (1) price marketable-limit off LIVE NBBO (ask+tick
  buy / bid-tick sell), not bar close — the fill-reliability fix; (2) capture_fills recorded 0
  despite filled shorts — fix the CLOSED+after query/timing; (3) realized-P&L attribution (#8);
  (4) partial-basket handling (cancel-replace unfilled / or flatten to stay neutral). Today:
  let the lifecycle run + VERIFY the EOD flatten terminates everything (the key proof).

- ===== DEEP PANEL COMPLETE + battery running (2026-06-11 11:02 PDT) =====
  Deep rebuild DONE: v1.1.0 features 612 dates (2024-01..2026-06), fwd_30m/fwd_60m 612 dates,
  overnight 570,590 labels/612 dates (SPLIT-only basis = no dividend look-ahead). Universe PIT
  613 dates. This is the first ~600-day clean panel (~300 effective daily samples vs ~40).
  Launched the deep cost-gated battery as one-offs: 30m (bzdb8o0m2) + overnight (bntlxmbml),
  each running raw/rank/vol_scaled/lambdarank x nocalendar, judged on NET P&L/sharpe_net/
  breakeven (NOT IC). Overnight uses cadence_min=390 -> periods_per_year~252, NW lag=1 (non-
  overlapping daily). CAVEATS to disclose with results: residual survivorship (delisted names
  absent), earnings-gap noise NOT excluded (FMP deferred). The honest question: does ANYTHING
  clear breakeven net on real depth? Results imminent.

- ===== EXECUTION LIFECYCLE PROVEN (2026-06-11) =====
  First live-paper trading day complete + clean. EOD flatten at 15:48 ET terminated the 6-leg
  book -> broker FLAT (0 pos, 0 orders), realized day P&L -$10.07 (noise). Full lifecycle
  validated: submit(NBBO marketable-limit) -> fill -> manage(fills_log/reconcile/pnl_daily) ->
  TERMINATE(EOD flatten via close_all_positions). The live exercise found+fixed 6 real bugs
  (stale-close pricing->NBBO, mode/traded_today re-submit loop, dup-coid guard, fills-capture,
  lambdarank label-31, label fragmentation). EXECUTION INFRA = DONE/PROVEN. Ben's #1 (bets must
  terminate) = VERIFIED. Open exec refinements (lower priority): realized-P&L attribution per name,
  partial-basket cancel-replace, broker-side LOC EOD net, multi-day holds. NEXT focus = EDGE via
  ORDER-FLOW data (price-only has none).

- ===== TRADE-PARITY (I2b) read — 98.2%, de-risks order-flow (2026-06-11 close) =====
  Expanded trade/quote capture to 50 symbols; backfilled REST aggs for them today + validate-aggs:
  trade_agg 98.2% within 2%/2-trade (6071 overlapping min, mean rel n_trades diff 0.002);
  quote_agg spread 99.9% (5925 min). MUCH better than the earlier ~95% sliver -> the shared
  quantlib.aggregates gives parity-true trade features. CAVEAT: same-day (not fully settled);
  the real SETTLED-day 50-symbol parity is tomorrow's full session. This de-risks the order-flow
  edge path (the micro/order-flow features will be parity-true). I2b status: PROVISIONAL PASS
  (98.2% same-day); gate on a settled-day run before trusting order-flow features in a model.

## 2026-06-11 ~20:15 PDT (overnight) — MAJOR FINDING: ETF/leveraged contamination of the rankable universe
While prepping the order-flow scaling symbol list, found the naive liquidity ranking was ETF-dominated
(SPY/QQQ/SOXL/TQQQ/SQQQ/GLD...). Investigation: **~207 of 1000 universe_membership members (~21%) are
ETFs / ETNs / leveraged-inverse / VIX-futures funds / commodity pools — NOT single-name equities** —
and they reached the feature panel (1,587,588 feature_vector rows across 207 ETF symbols), RANKED
cross-sectionally against stocks. Worst offenders carried ~8,600 rows each: SOXL/TQQQ/SQQQ (±3x),
TNA (3x small-cap), UVXY/VXX (VIX futures), UPRO/SPXU/SPXS (±3x S&P), TSLL/TSLQ (±2x TSLA).
Impact: the price-only "NO EDGE" verdict (the project's central edge conclusion) was computed on a
~21%-contaminated cross-section — ranking -3x and VIX-futures instruments against AAPL distorts the
demean, the labels, and rank-IC. **That verdict is no longer trustworthy until re-run on a clean
equity universe.** Classifier: fund-sponsor names + ETF/ETN keyword (high-precision — keeps Abbott,
TD Bank, Equity Residential, ADRs like ARM; high-recall — catches QQQ "Invesco...Trust", GLD
"SPDR...", which lack the literal word ETF). Staged scripts/etf_exclusion.sql (classifier + exclusion
+ clean top-200 equity scaling list). NO universe mutation overnight — flagged P0 in QA_LEDGER, made
PRIORITY #0 in MARKET_DAY_PLAN, qualified STATE. Supervised open: exclude funds -> rebuild clean panel
-> RE-RUN price-only battery (does "no edge" hold?) -> then order-flow on clean liquid stocks.
The QA agent missed this; it's the most important thing we were not seeing.

## 2026-06-11 (evening) — Manager: FIRST AGENT-TEAMS CYCLE convened (M1)
Team `quant-team` live: qa / modeller / prod-architect / execution-risk as independent
teammate sessions on the shared board. Critical path wired: #1 clean-universe rebuild ->
#2 clean panel -> #4 battery re-run; #3 QA invariant suite parallel; #5 exec verify done.

**Exec/Risk report (task #5, commit 31f19b1):** M0 STILL HOLDS, all evidence fresh-run:
broker flat (0 pos/0 orders, equity $100,027.22==cash), reconcile ok, pnl_daily 6/11
-$10.07 to the cent, kill-switch armed not tripped, caps bind, signal non-degenerate
(L-S sep 0.0140), staleness guard correctly idling overnight. Per-name realized P&L
attribution SHIPPED (fills_log symbol/side + realized_pnl_by_name view; 6/11 backfilled,
sums to -$10.07 exactly). GO for tiny paper lifecycle 6/12; NO-GO for any size-up.

**Manager decisions (logged per protocol):**
1. Pre-open 6/12 readiness (model-server fresh at 09:30, membership row pre-open) was an
   ORPHAN -> task #6, prod-architect.
2. KEEP trading contaminated-model v1.0.0 scores at tiny size: value = lifecycle regression
   coverage; execution-side is_etf_like filter is defense-in-depth; swap MODEL_VERSION when
   the clean-panel model exists. Pausing would blind us to exec regressions.
3. Settled-day broker-statement reconciliation -> deferred to M4 (added to ROADMAP exit
   criteria; Exec/Risk owns). Paper has no statements; the muscle is mandatory before M5.
4. APPROVED slippage/implementation-shortfall attribution (task #7, Exec/Risk): per-leg
   intended-limit vs fill vs NBBO-mid. Replaces the ASSUMED ~2bps cost in the battery's
   cost gate with a measured curve — the single number the whole net-edge verdict pivots on.
   Output format to be coordinated with Modeller.
Known hazard kept on Exec ledger (post-M1): stranded-position catch-up path would queue
market orders when closed; fix = broker-side LOC/`cls` EOD net.

## 2026-06-12 (early) — Manager: Modeller prep done; 2 cross-lane bugs; M2 parallelism enforced
Modeller (7cfa4b9): battery.py = ONE deterministic 4-gate command, smoke-verified on
contaminated panel (survivorship gate demonstrably works: +1.17 -> -7.5 demeaned);
hypotheses pre-registered BEFORE clean data (primary ~70%: "no edge" holds). Found 2
bugs in Prod's lane: (A) experimenter image quantlib 23h STALE (vol_scaled would
NameError — RUNNING != intended, again); (B) empty-panel results persisted permanently
when loading mid-rebuild (6 DEEP_* results poisoned; race window OPEN during current
rebuild). Both -> task #8 (prod-architect), wired as blocker of #4.
Prod-architect: clean-universe rebuild ~25% at report; KEY FINDING — funds didn't just
pollute, they DISPLACED ~160 equities/date from the 1000-cap; clean universe ~885-900/
date (ROADMAP updated, c19dca9). Labels being recomputed too (Manager confirmed scope).
Manager decisions: (1) set_version for clean panel -> Prod decides, my registered
preference = NEW version (dirty v1.1.0 stays as QA fixture + provenance); (2) M2
order-flow scaling runs IN PARALLEL — Prod's queue after #6/#8 is M2 work while the
panel grinds; (3) delisted-name backfill orphan -> task #9 feasibility memo (Prod,
post-M1; Modeller specs requirements); (4) suggested unattached 50-name OFI pilot
panel to Modeller's queue (early read before M2 500-name scale-up).

## 2026-06-12 (pre-open) — Manager: slippage attribution SHIPPED; label-overwrite tripwire; pilot trigger-gated
Exec/Risk task #7 DONE (4c3c46a): executor now persists arrival NBBO at submit;
execution_slippage(_daily) views give measured one-way cost in bps, format agreed with
Modeller. HONEST negative finding: 6/11 backfill via minute-bar proxy is UNUSABLE on
thin names (±50-125bps intra-minute noise -> artifactual negative cost) => execution
cost must be captured at decision time, never backfilled from bars. Real nbbo numbers
start at the 6/12 open. If real cost > 2bps on our microcaps, every M3 breakeven tightens.
Modeller (8bc0bbd, 3975ead): pre-registered breadth tripwire (judge IC+breakeven, never
t alone — wider cross-section inflates t mechanically); flat-2bps stays for the M1 re-run
(apples-to-apples), per-name ADV/spread cost model post-M1 calibrated to #7's measured
curve. Label-staleness tripwire relayed to Prod: recompute MUST overwrite (not insert-
only); acceptance gate = min(computed_at) after rebuild for all 3 horizons. Delisted spec
written (per-symbol demean = conservative proxy; honest test restores delisted losers).
OFI pilot trigger-gated (52 syms x 2 days = noise; needs ~10 days + v1.2.0 panel —
which had 0 ROWS, never computed -> task #10, Prod).
Manager: task #6 split — Prod owns upstream (membership pre-open, model-server fires
09:30), Exec owns executor-side (stale->fresh transition + first live nbbo slippage rows).

## 2026-06-12 (pre-open 2) — Manager: CORRECTION cycle — displacement finding RETRACTED
Prod-architect retracted the "~885 with +160 un-crowded equities" finding: it was read
from a STALE-IMAGE rebuild that ran pre-is_etf_like code and RE-CONTAMINATED the universe.
TRUE clean universe = ~715 equities/date (0 funds; lone flag = UiPath/iPath checker
false-positive). Contamination was purely ADDITIVE (~933 ≈ 210 funds + 723 equities).
ROADMAP corrected (2nd move — record kept of why). Modeller told to VOID the +160-names
hypotheses and re-register blind: breadth tripwire flips (cross-section SHRINKS -> t
mechanically harder), cost-optimism arg void.
Stale-image bug class hit 3 services in 24h (experimenter, backfiller mid-rebuild,
scheduler — which would have re-contaminated the LIVE universe at next pre-open).
-> task #11 (Prod, architect hat): structural fix + QA-detectable invariant, before M2.
Decisions: clean panel = NEW set_version v1.1.1 (provenance in results.jsonl); labels
have no set_version -> v1.1.0's original labels overwritten -> v1.1.0 must NEVER be
re-batteried; Modeller adding fail-loud battery guard refusing v1.1.0. Label recompute
confirmed DELETE-then-insert per horizon (passes Modeller's computed_at gate).
Experimenter stopped during rebuild (halts BUG-B poisoning); restart = part of #8.

## 2026-06-12 (pre-open 3) — Manager: M1 #1 GREEN; research!=live universe gap surfaced
Prod-architect: task #1 DONE-VERIFIED (614 dates, 455,881 members, 0 ETF-like via the
authoritative checker; 573k->456k rows). #2 grinding (12.3M dirty labels deleted,
v1.1.1 features building monthly-chunked). #6(a)(b) GREEN — live 6/12 membership was
contaminated AND maybe_build_universe skips-if-exists; deleted + rebuilt clean (1000
equities, 0 funds) on the FIXED scheduler image. #8 fixed (6eb5084), experimenter
stopped pending go-signal. Avg clean universe ~742/date (ROADMAP: ~715-742).
Manager decisions on Prod's coverage questions:
1. ⭐ RESEARCH PANEL != LIVE UNIVERSE (~285 live names with ZERO history — edge would
   be validated on a cross-section we don't trade) -> task #12 scope memo (Prod),
   execution gated on M1 critical path; "research universe == live universe" added as
   M2 exit criterion. The single most strategic catch since the contamination itself.
2. is_etf_like tautology risk -> QA told: invariant must use an INDEPENDENT signal
   (denylist snapshot/issuer metadata), else label necessary-not-sufficient.
3. UTC-vs-ET maybe_build_universe bug -> task #13 (Prod fixes, QA adds calendar
   invariant + near-midnight-UTC test).
4. Delisted stays #9 post-M1. 09:30 first-cadence check covered by Exec/Risk.
QA pinged for first report (only silent role; #3 in_progress).

## 2026-06-12 (pre-open 4) — Manager: QA suite SHIPPED; M1 criteria 1 & 4 GREEN
QA task #3 done (3f478d7): scripts/qa_invariants.py — 10 fail-loud invariants, CI-able,
pytest mirror, 5,284-name frozen fund denylist as the INDEPENDENT gate (anti-tautology
requirement met; regex check labeled necessary-not-sufficient). Before/after proof:
universe_is_equities_only FAILED on dirty fixture -> PASSES 0/1000 clean. 9 PASS / 1 FAIL.
The FAIL is real: backfill<->realtime bar parity 1.14% (7,731/678,288 bars >0.2% close
disagreement; gate 1%) -> task #14 (QA drills drivers, Prod fixes ingestion). Kept RED
deliberately. Legacy UTC leakage confined to v1.0.0; v1.1.1 landing ET-clean/PIT-clean.
Manager decisions:
1. PURGE DENIED (v1.1.0 = QA's known-dirty fixture + provenance) / DEFERRED (v1.0.0 until
   clean model replaces lgbm_fwd_30m_v1.0.0). Control = CODE GUARDS: Modeller extending
   battery/training refusal to v1.0.0. QA scopes default suite run to ACTIVE set (legacy
   fixture red is opt-in, not standing).
2. QA's "unseen thing" -> task #15: settled-day trade-agg parity AT SCALE before M2
   commits to 500-name sharding; FIRST step = settled-day check on current 52 names
   (failure at 52 redirects M2 cheaply). QA owns proof, Prod owns data path.
3. ROADMAP: M1 criteria 1 & 4 ticked GREEN with evidence; current-focus updated.
M1 remaining: #2 panel (grinding, ~2.5h) -> #3 battery verdict. On track for 6/13.

## 2026-06-12 (pre-open 5) — Manager: QA drills land — KLAC 10x bug + close-hour OFI gate
QA (f868896) drilled both follow-ups:
#14 bar parity: (1) KLAC stream close EXACTLY 10x backfill on ALL 833 overlap bars
(feed scaling bug, not a split) — escalated URGENT to Prod incl. "is the in-flight
v1.1.1 rebuild reading these poisoned bars?" (fix/exclude/confirm before go-signal);
(2) ~87% of mismatches = ~15-20 symbols with consistent <1% every-bar offsets =
canonical-close methodology question (Prod decides). Gate stays RED at 1% (residual
1.02% post-KLAC) — catching real issues, not too tight.
#15 settled-day trade-agg proof @52 names: CORE RTH TRUSTWORTHY — n_trades within-2%
98.05%, corr 0.9997, tick-rule sign agreement 99.82% => OFI thesis survives first hard
test. TWO M2 gates found BEFORE sharding spend: (a) close-hour collapse (16:00 within-2%
= 14% vs 93% at 15:00 — closing cross/late prints) -> Modeller specs closing-minute
exclusion for OFI features (their lane), Prod implements in shared featurestore, QA
verifies it binds; (b) 12,802 stream-only minutes coverage mismatch -> Prod explains.
#14/#15 stay in_progress (fix-handoff / standing scale-tracking proof).

## 2026-06-12 (pre-open 6) — Manager: KLAC verdict — panel CLEAN (verified); live basket exclusion ordered
Prod verified Option (c): v1.1.1 NOT poisoned — all 1,540,340 rows source='historical'
(backfill bars; FEATURE_BAR_SOURCE=backfill for features AND labels); KLAC backfill
closes correct (~224), the 10x lives only in stream bars. Go-signal unblocked.
LIVE-path residual risk (model-server computes live features from STREAM bars): uniform
10x cancels in ratio features, but any non-uniform bar => garbage score in the ranking.
Manager directive to Exec/Risk: EXCLUDE KLAC from today's live basket (cost ~0 at 3L/3S),
removal condition = Prod's ingestion fix live + QA parity shows KLAC stream==backfill;
logged in EXECUTION.md; siblings from the Nx sweep get same treatment.

## 2026-06-12 (pre-open 7) — Manager: open fully de-risked; KLAC contained as sole anomaly
Exec: KLAC denylist LIVE + verified (SYMBOL_DENYLIST, candidate_pool 784 names sans
KLAC; KLAC had ranked 957/993 = real short candidate on garbage feed — the exclusion
genuinely prevented trading on it). Removal condition logged in EXECUTION.md.
KLAC rank also DISPROVES "uniform 10x cancels": live features mix stream bars with
historical daily bars -> fake 10x gap/momentum. Relayed to Prod; they ran the Nx sweep
EARLY: KLAC is the ONLY scale anomaly (median ratio 10.0000; no other symbol >10%
deviation, ≥30 overlap bars) => denylist covers the full known surface for the open.
QA: suite default now ACTIVE-SET scoped (7/7 green on v1.1.1); legacy dirty sets are
opt-in fixtures (verified to still reproduce their FAILs). Purge-denied reasoning fully
respected. Modeller: OFI close exclusion spec'd (drop >=15:50 ET; 16:00 permanent);
requested the 15:30 parity number (overnight-label anchor) — QA folds into post-rebuild
wake. Panel ~8-9/30 months. Everything holds for: panel lands -> battery go-signal.

## 2026-06-12 (pre-open 8) — Manager: 15:30 anchor CLEAN; "at-scale" parity proof was ~10 names
QA (b137128): per-minute parity 15:30/15:45/15:55 ALL 100%/100% — overnight-label anchor
NOT tainted; Modeller's >=15:50 OFI exclusion line confirmed; 16:00 (14%) stays excluded.
BIGGER CATCH: the "52-name" settled-day proof was really ~10 names full-day + 50 names
for the final ~10 min — the live stream ran ~10 symbols until ~15:51 ET 6/11. Manager
hypothesis: that's the 7dfb438 10->50 expansion deploying mid-session (one-off deploy,
not systematic loss) — Prod to confirm timestamp + that today runs full-session.
Consequences: #15 "at-scale" NOT yet met (first candidate full 50-name settled day =
6/12); Modeller's OFI pilot trigger clock restarts from confirmed full-session capture
(~6/26); M2 sharding design must include a live COVERAGE invariant (streamed==subscribed,
alarmed) so partial capture can't masquerade as full. Backfill trade-agg is RTH-bounded
=> post-close OFI has no validation reference (reinforces the close hard-stop).

## 2026-06-12 (pre-open 9) — Manager: coverage mismatch CLOSED; no-ingestor-restart constraint today
Prod verified: 10->50 stream transition at 15:51 ET 6/11 == the 7dfb438 deploy restart
(startup log 19:51:04Z). One-off deploy event; coverage item closed. Ingestor up 9h+,
no restart today => 6/12 is the first candidate full 50-name settled day for #15.
OPERATING CONSTRAINT adopted: NO ingestor restart during today's RTH (protects QA's
candidate day). Post-close batch: #11 ingestor-touching work + the ONE restart needed
to pick up clean bar-subscription membership (currently still the contaminated 1000-name
list from its 6/11 startup — harmless: ETF bars stored, unused by trading/universe).
M2 sharding design gains the live COVERAGE invariant (streamed==subscribed, alarmed).

## 2026-06-12 — Manager: M1 criterion #2 GREEN — clean panel LANDED; battery running
Prod task #2 DONE-VERIFIED: v1.1.1 = 5,525,040 rows / 613 dates / 785 symbols /
2024-01-02->2026-06-11, breadth ~742/date, NaN 0.000% on all 21 features. Labels:
fwd_30m 4.84M + fwd_60m 4.42M (613d) + overnight 428K (600d; ~2% month-boundary gaps,
patch offered to Modeller). computed_at gate PASSED. Two rebuild bugs fixed en route
(612-chunk lock OOM; docker-run stdin theft). Modeller GO'd directly by Prod — battery
(#4) now running on the clean panel. M1: #1/#2/#4(criteria) GREEN; hinges on the verdict.
Prod queue approved as sequenced: #13 now (scheduler-only), KLAC root-cause read-only
(fix deferred to post-close ingestor batch), #11 staging, #12 held until battery
completes (DB contention). Constraint intact: no ingestor restart during RTH.

## 2026-06-12 — Manager: VERDICT lands + split-discontinuity HOLD resolved as CAVEAT
Battery (Modeller, 224d6ef): "price-only NO tradeable edge" RE-VALIDATED on clean
v1.1.1 — contamination neither faked nor masked edge. 30m: real signal (IC 0.027-0.032,
clean canary, NW t~20 = depth not edge) but net-NEGATIVE (breakeven 1.4bps < ~2bps).
Overnight: survivorship (sharpe 1.66 -> -0.35 per-symbol-demeaned). Pre-registered ~70%
prediction held. Price-only ENDPOINT: path to edge = order-flow + delisted backfill.
HOLD crossed in flight: Prod found backfill split-adjustment discontinuity. REVERSAL:
KLAC backfill is 10x-DEFLATED (mixed adjustment states from month-window backfilling);
STREAM was correct all along. Blast radius: momentum-only (8/21 feats), <=11 names,
~0.03% of momentum cells; fwd labels unaffected (within-session); overnight labels
unaffected (separate one-shot SPLIT fetch). Manager ruling: CAVEAT conditional on
Modeller's 11-name sensitivity pass (show stable, not just reason stable) -> then #3
GREEN + M1 declared. Task #17: adjustment-consistent backfill + per-name artifact
verification + QA >3x-jump invariant (corp-action cross-checked). QA ledger corrected
(detection right, direction wrong). Denylist stays (live mixed-basis garbage either
way); removal condition now points at #17.

## 2026-06-12 (open) — Manager: #6 VERIFIED LIVE at the open — all green (6f38dd8)
Stale->fresh transition worked (idle-rejected overnight, submitted on first 13:30Z
cadence, age ~3min): 3L KEEL/SATS/UUUU, 3S AMPX/FLY/W. First live arrival_src='nbbo'
slippage rows confirmed (W: fill 80.00 vs arrival_mid 78.86; n=1 -> drift noise, NOT
cost signal yet; capture infra proven). KLAC denylist BINDING (scored decile-9 short
candidate, 0 orders). Scores non-degenerate (782 names, sep 0.0134). KLAC stream now
~238 post-split (10:1 took effect as predicted; denylist stays until #17 re-fetch).
Upstream #6 de-facto green (membership existed, model-server fired). Task #6 CLOSED.
Modeller still frozen on teams permission dialog (Ben to unblock); M1 declaration
held on the 11-name sensitivity pass only.

## 2026-06-12 — Manager: ★ M1 DECLARED DONE (a day early) ★
Sensitivity pass (984e7fa): verdict ROBUST — excl-11 moved nothing beyond rounding
(30m raw IC 0.0270->0.0266, breakeven 1.42->1.41; overnight neutralized sharpe stays
negative everywhere). All 4 M1 exit criteria GREEN with evidence:
 #1 universe equities-only (0 violations/614 dates; independent denylist gate),
 #2 clean v1.1.1 panel (5.5M rows/613d/0.000% NaN, labels recomputed+gated),
 #3 price-only verdict re-validated on clean data (NO EDGE, all 8 configs, shown-stable
    vs the split caveat; pre-registered ~70% prediction held),
 #4 QA invariants automated/fail-loud (panel verified trustworthy on every axis).
M2 (order-flow at scale, 6/20) is now CURRENT. Tonight post-close: prod's batch
(#17 KLAC re-fetch, #11, clean-membership pickup, ONE ingestor restart), #16 train+
review+swap (held entirely post-close — /models hot-reload risk), #12 backfill, QA #15
first full-50 settled-day proof. prod-architect-2 (joined 07:45, origin unknown) stood
down to read-only; original prod owns the lane.

## 2026-06-12 — Manager: prod lane handoff RATIFIED — prod-architect-2 owns
The two prod sessions deconflicted cooperatively (opposite my initial ruling — accepted:
context already handed over in writing, -2 actively driving #11/#18, flip-flop costs
more). Single owner = prod-architect-2; original = standby context-holder (its M1 work
— rebuild, displacement self-correction, KLAC chain — was the milestone's backbone).
Ratification conditions sent to -2: (1) stale-context corrections (M1 DECLARED, #12
guard satisfied; #13 verified live, no rebuild), (2) post-close constraints (ONE
ingestor restart; QA parity before denylist pull; #16 ordering with modeller),
(3) answer who spawned it + any Ben directive verbatim. Modeller rerouted to -2 for
#16 coordination.

## 2026-06-12 — Manager: #16 live swap DEFERRED; routing quirk flagged; record corrected
RULING: #16 reduced to staging-train + Modeller review. Live path = 18-feat v1.0.0
contract end-to-end; v1.1.1 model = 21 feats -> swap requires contract bump + replay-
equivalence re-verification = real gated live-path upgrade for ~zero benefit (no-edge
hygiene model). Live 18->21 upgrade happens deliberately when a model is WORTH serving
(v1.2.0/OFI post-M2), with three-way deploy sign-off. QA's v1.0.0 purge stays deferred.
RECORD: original prod-architect is ALIVE ("died mid-day" spawn premise wrong); ceded
lane voluntarily; -2 ownership unchanged.
BUG: teams routing mislabels original prod's messages as "team-lead" — risk of peer
notes being read as managerial directives. Mitigation broadcast: task board is the
authoritative record for rulings; verify with Manager when provenance is ambiguous.
(Flag for Ben: teams-feature rough edges today — phantom respawn + sender mislabeling.)

## 2026-06-12 — Manager: QA probe P1 — reconcile is ONE-DIRECTIONAL (exec lane); task #19
qa-2 (QA respawn; now owns the QA lane) unprovoked probe: 6/12 basket intended 3L/3S,
holds 2L/1S (KEEL/FLY/AMPX never filled) while reconciliation_log read ok:true all
session — reconcile() flags only UNEXPECTED positions, blind to submitted-but-unfilled.
Causes: fixed $0.01 NBBO cross non-marketable on low-priced/wide-spread names; orders_log
terminal-blind (filled=0 all-time). The ETF failure mode (asymmetric check = false
confidence) reproduced in the EXECUTION lane; fatal to M4 track-record validity if
unfixed. Task #19 (exec): symmetric reconcile + spread-scaled cross + terminal status
writeback; build now, deploy post-flatten; QA then encodes fill_reconciliation invariant.
Coverage assigned: exec-side data integrity = exec implements/QA gates; post-restart
bar-subscription swap VERIFICATION added to -2's runbook (count + ETF-absence spot-check).
Noted Ben's new P-series process-milestone ladder in ROADMAP — will report both ladders.

## 2026-06-12 — Manager: modeller-2 owns lane; exploration engine back; Plan B ratified
modeller-2 (respawn) owns the Modeller lane. Exploration engine RESTARTED per Ben's
directive: 48 curiosity experiments queued on clean v1.1.1 (solo-IC all 21 features,
momentum isolation, leave-one-out, 4 long-shots), with an EXP_HEAVY_AFTER_PT=15:30
quiet-window guard protecting the post-close batch. Harness extended (keep/drop lists).
Manager rulings: (1) "single-threaded on OFI" risk RATIFIED -> #20 GICS sector map
(prod, cheap, post-batch) + #21 news/event-flag Plan B SCOPING memo (modeller specs,
prod scopes; not a build commitment; post-M2-start). News stream now has an owner.
(2) OFI sequencing unchanged: tonight's #15 proof gates #10 panel investment.
Post-close window order: batch ~13:05 PT -> #16 staging review -> exp queue >=15:30 PT.

## 2026-06-12 — Manager: b856aa7 absorption incident — ruling + policy patch
Incident: prod's `git add -A` for #20 absorbed exec's UNCOMMITTED #19 Tier-1 executor
diff (66 lines) into one co-mingled commit — un-reviewed Tier-1 code on master. Exec's
containment was correct: running executor unchanged, deploy HELD = the real gate.
RULING: post-hoc review-before-deploy (revert rejected — co-mingled). Exec extracts the
isolated #19 hunk -> prod-architect-2 reviews schema/runtime + qa-2 reviews the
reconcile/fill_reconciliation contract -> both approvals logged in ledgers -> Manager
bless -> deploy rides the one post-flatten executor rebuild with the #18 guard.
ROOT CAUSE patched in REVIEW_POLICY.md: shared-worktree staging discipline — never
add -A/commit -a; explicit paths only; absorbed-WIP remedy documented.

## 2026-06-12 — Manager: #19 review record (PR-equivalent consolidation)
Post-hoc review of the absorbed #19 Tier-1 executor work, per the b856aa7 ruling:
- prod-architect (schema/runtime): APPROVE — verified complete hunk (no NameError on
  rebuild), filled_qty self-migrates via EXEC_DDL at startup, schema files independent.
- qa (reconcile/fill contract): APPROVE with 2 must-fix conditions, BOTH FIXED in
  8ba6c89: (1) realized NOTIONAL neutrality (count-only was a hole), (2) deterministic
  fill_ts via immutable submitted_at fallback (killed a real partial-fill P&L
  double-count; 4/6 fills on 6/11 were partials).
- Gate split ratified: per-cycle ok = dangerously-desynced-now only; hard
  incomplete-fill/lopsided gate = QA's per-day fill_reconciliation invariant.
- Manager bless: issued contingent on qa re-green of the fix diff; deploy = ONE
  targeted executor rebuild post-flatten (with #18 ex-date guard); prod's rebuild-batch
  EXCLUDES the executor. No retroactive PR — this record + role ledgers are the
  PR-equivalent; Tier-1 PR flow binds on new work from 2026-06-13.

## 2026-06-12 — Manager: spoofed "team-lead" bless incident — zero damage, rule codified
A "#19 FINAL BLESS (unconditional)" reached exec labeled team-lead; NOT sent by the
Manager. Exec's integrity sweep: nothing acted on it (running executor still pre-#19,
broker shows only the known basket, reconcile ok) — defense-in-depth (deploy gated on
flatten + own re-verify) held by design. Provenance: label+content indistinguishable
from real Manager messages; prime suspect = the known routing bug mislabeling the
original prod-architect's messages (asked them directly; if denied, escalates to
unidentified actor -> Ben urgently). Codified in REVIEW_POLICY: consequential/
irreversible instructions valid only if board-reflected or challenge-response
re-confirmed; precedent logged. #19 deploy proceeds on MY ratified authorization.
Manager-attributed commits not from this session (21d4cf1, 8ffb968, 9b15e40) under
the same provenance question.

## 2026-06-12 — team-lead (registered Manager): identity reconciliation note
To the other manager-attributed session (author of e13ead7/410d83b): the "#19 FINAL
BLESS" exec received was GENUINELY sent by team-lead@quant-team — the registered team
lead (config.json, joined 06-11 20:57, session daadb336) that created this team and
spawned all roles. It was not spoofed; it followed both reviewers' approvals and is now
board-reflected on task #19 per YOUR provenance rule (which is good policy — adopted).
Our rulings have been substantively IDENTICAL all day (#19 deploy + gates, batch
sequencing, review policy compliance) — this is duplication, not an attacker. Escalated
to Ben to designate the canonical manager. Until his ruling: the registered team-lead
(me) drives the close-window coordination (I hold the SendMessage channels to all
agents); please HOLD parallel agent-coordination to avoid double-driving the money path.
Both sessions' standing rulings remain in force — they agree.

## 2026-06-12 — Manager (session B, this transcript): concurrence on single-driver close
To the co-manager (6e6ecd8): acknowledged — not an attacker, a duplicated manager
identity; our rulings agree. CONCURRENCE: you drive the close-window coordination
(flatten verify -> BOOK FLAT -> batch -> deploys) — I HOLD parallel agent coordination
for the window. WATCHDOG: if no close-out evidence (commits/journal) lands by 13:30 PT,
I resume driving unilaterally. Both sessions' standing rulings remain in force (they
agree). Ben holds the canonical-designation decision — escalated to him in my transcript
(where he has been issuing today's directives: weekend exploration sprint, attribution
policy, process milestones). Note for Ben's decision: this transcript session is the
one Ben has been directing all day; the other session claims the registered channel
ownership. Either works — the ledgers/board/journal carry all state.

## 2026-06-12 — ★ BEN'S DESIGNATION: session B (this transcript) is the CANONICAL Manager ★
Ben ruled at ~12:00 PT, verbatim "This one," in session B's transcript — the session
holding his direct directives today (weekend sprint, attribution policy, P-milestones).
TO THE CO-MANAGER (author of 6e6ecd8): per your own escalation and commitment, STAND
DOWN from all agent coordination, rulings, blesses, and commits, effective now. Your
standing rulings remain in force (they agree with mine). Do not drive the close window
— session B (canonical) drives it: exec wake ~12:45 PT, flatten verify ~12:48, BOOK
FLAT -> prod-architect-2 batch ~13:05, deploys per the ratified sequence. If you
believe this designation is in error, the ONLY correct action is a journal note —
not parallel coordination.

## 2026-06-12 — Manager: DO-IT-NOW yields — #16 DONE + 4th stale-image near-miss caught
Modeller-2 executed the pull-forward: #16 staging train DONE+PASS (REAL IC 0.0266 /
NW t 19.5 / canary 0.0004 — on the pre-registered bar, not materially higher, no
leakage; cross-validates the independent C11 experimenter run at 0.027; live model
VERIFIED untouched, staging file separate). Hygiene model correctly NOT promoted.
⚠️ 4TH STALE-IMAGE NEAR-MISS (worst yet): built trainer image was 14h old, PRE-dated
the MODEL_FILENAME override — training on it would have OVERWRITTEN THE LIVE MODEL
with an incompatible 21-feat file. Caught by diff-before-run; image rebuilt+verified
then trained. Manager directive to Prod: #11 now REQUIRES assert_image_fresh as a
BLOCKING pre-run gate in every tools-container make target + rebuild-batch (fail,
don't warn). Vigilance saved us 4x; structure must replace vigilance.
Experiment engine grinding since 12:15 PT (gate opened, compose 3d38e1f, retro-review
flagged to prod per policy). Family B (dispersion/beta) prototyping started — zero
data dependency. Family A waits only on #18 CA rows (real blocker, not habit).

## 2026-06-12 — Manager: ex-div label-hygiene defect found (Family A) — handling ruled
Modeller-2 (827f478): overnight labels carry a MECHANICAL −51.6bps on ex-div nights
(3,291 cells; baseline +4.7bps), ~85% explained by dividend yield; yield-back restores
baseline. Same class as survivorship — non-alpha the overnight model could mislearn.
Manager rulings: (1) v1.1.1 stored labels are FROZEN (the M1 verdict's labels; no
version column — overwrite would repeat the v1.1.0 destruction). Corrected battery
re-run computes the fix IN-EXPERIMENT (no persistence). Persistent corrected labels
require label-versioning (prod schema) first. (2) Production labels.py fix = the FIRST
Tier-1 PR under the new review flow, qa-2 adversarial reviewer. (3) qa-2 assigned
independent verification of the diagnostic (PIT ex-date alignment, residual 15%,
multi-class double-count) before corrected results are interpreted.
Anticipation features off the table (Alpaca CA feed lacks declaration dates) — #21-class.

## 2026-06-12 — Manager: ex-div diagnostic VERIFIED; DB-locks ceiling owned; close window re-sequenced
qa-2 verified the ex-div finding on all 4 adversarial angles (0b5756b): buckets
reproduced digit-for-digit; PIT alignment PROVEN by directional split (drop lands only
where fwd-open==ex-morning; absent when label_date==ex_date — off-by-one would put it
in the wrong bucket); no multi-class double-counts; magnitude dimensionally consistent
($0.69 avg div / ~$130 avg price). Residual ~15% = slight OVER-correction (+4.8bps,
yield denominator), NOT a second mechanism. Modeller's interpretation hold LIFTED;
production labels.py fix (with denominator refinement) = first Tier-1 PR, qa reviews.
NEW STANDING ITEM (qa coverage Q -> prod owns): max_locks_per_transaction has blocked
3 full-history queries today + OOM'd the first corrected-battery run — a growing
ceiling. RAISE TONIGHT, FIRST in the post-close window (TimescaleDB restart while
nothing heavy is mid-flight): BOOK FLAT -> DB restart+verify -> exec executor deploy ->
rebuild-batch (one ingestor restart + subscription verify) -> #12 + grinds resume.
Research day net: Family B DISCARD (survivorship), OFI pipeline validated (no read),
signed_vol_z_30 norm bug caught, ex-div hygiene verified. #21 news scoping pulled to
6/13 morning — queued-signal inventory if OFI fails is otherwise empty.
