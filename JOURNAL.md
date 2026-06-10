# Experiment & Decision Journal

Append-only. Newest entries at the top. Experiments record: hypothesis, config
hash, out-of-sample result, verdict, next step. Decisions record what changed and
why.

---

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
