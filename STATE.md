# STATE — read this first

**Phase:** Phase 1 ~done; Phase 2 (feature engine) in progress
**Mode:** paper
**Last updated:** 2026-06-10
**Operating mode:** autonomous maintainer loop — see `OPERATING_LOOP.md`. Self-schedules
continuation; doesn't wait to be asked.

## >>> PRIORITY: thin END-TO-END vertical slice (Ben's directive 2026-06-10) <<<
Get the whole loop running once — backfill → train → deploy → paper-trade → reconcile —
even with a trivial model, to SEE E2E in action; then improve each piece. First run is
PLUMBING VALIDATION, not edge (don't trust its IC/P&L — thin panel, known caveats).
Slice steps:
1. [in progress] Rebuild panel over all 51 dates (per-date demean) so it's trainable.
2. [ ] Train a first LightGBM on the panel; save the model.
3. [ ] model-server: load model, score live feature_vectors each minute -> predictions.
4. [ ] executor: read latest predictions -> trivial L/S basket (paper), reconcile.
Keep the harness gates for the REAL run later; this slice is to validate the pipeline.

## Phase 2 progress
- [x] `quantlib/features.py` — v1 18-feature set, point-in-time, feature-level
      replay-equivalence test (12 tests pass total).
- [x] **Historical feature-store builder** (`backfiller build-features`): computes
      feature_vectors from stored bars + aggregates via quantlib.features, registers
      feature_set v1.0.0. Verified: 300 vectors for 5 symbols, all features sane.
- [x] **Live feature-computer** (`services/feature-computer`): computes
      feature_vectors (source='stream') each minute via shared quantlib.featurestore.
- [x] **On-real-data feature replay-equivalence: 100% identical** (stream vs
      historical-recompute). Phase 2 technical heart proven — no feature-level
      train/serve skew. Refactored shared logic into quantlib/featurestore.py.
- [x] **Forward-return labels** (`quantlib/labels.py` + `backfiller build-labels`):
      cross-sectional excess return vs universe median at fwd_30m / fwd_60m, gap-safe
      timestamp lookup, 5 unit tests. Verified sane (cross-sections median-centered,
      ~20bps std). Overnight horizon = later refinement.
- [x] Universe-wide features (30,970) + labels computed for today; **training_data
      view** (feature_vectors ⨝ labels) created (`db/init/02_views.sql`).
- [x] Priority-E sanity look logged (short-horizon reversal signature visible but
      statistically meaningless on 1 day — see JOURNAL).
- [x] Grafana graph #1: "Ingestion — Live Coverage" (symbols/min) at
      http://192.168.1.32:3001 (anonymous viewer). Add more ONE AT A TIME w/ Ben.
- [x] ML advisor proposal folded into docs/RESEARCH.md (first-wave E0-E4; cheap wins
      = vol-scale label + signed-volume z-scores; leakage canary + deflated Sharpe).
- [x] Data-quality fix: backfiller now requests split+div-adjusted bars (was raw).
- [x] **backfill-manager** (always-on service): self-maintains bar history to a
      target depth (BACKFILL_TARGET_DAYS, currently 90; raise toward 6yr later).
      Walks month windows oldest-first, adjusted bars, resumable via backfill_windows,
      idempotent upsert, rate-limited. Shared fetch in quantlib/barsource.py (reused
      by the one-shot backfiller). Replaces the manual hand-launched backfill.
- [ ] Let history accumulate toward target; raise target once proven stable.
- [ ] **TOP PRIORITY (critic-flagged bug, before any modeling): point-in-time universe.**
      Build universe_membership per historical trade_date (screen from backfilled bars
      via quantlib.universe), then make build_feature_store/build_labels select per-date
      membership and demean labels within that date's universe. Rebuild the panel.
      Reason: current builders use max(trade_date) universe → survivorship bias.
- [x] **RTH + session_open + timestamp-safe features (DONE).** is_rth() DST-correct;
      featurestore filters bars+market to RTH (session_open = first RTH bar); features.py
      uses timestamp-based gap/session-safe lookups; label price series filtered to RTH
      (forward returns RTH-to-RTH). 19 tests pass.
- [x] Backfill complete to 90d; breadth UNIFORM (989-1000/weekday back to Mar 9).
      Breadth gate = trivial (weekday + min-symbol), not a subsystem.
- [x] DST regression fixed: minute_of_day/day_of_week now ET-local (were raw UTC).
- [x] **Point-in-time universe history built** (51 dates, strictly-prior ADV from local
      bars, no lookahead; membership varies by date). `backfiller build-universe-history`.
- [ ] Wire build_feature_store + build_labels to select per-date universe_membership
      and demean labels within each date's universe; then rebuild the source='backfill'
      panel. (Decision pending: 13 non-micro features for the full universe vs keep 18
      w/ NaN — micro is subset-only at ~98% NaN universe-wide.)
- [x] **Modeling-harness mechanics built** (`quantlib/backtest.py`): purged/embargoed
      walk-forward (purge by label horizon in market time), within-timestamp Spearman
      rank-IC, within-group shuffle-label canary, Newey-West deflated t-stat. 6 trap
      fixtures pass (26 tests total). Model-pluggable; LightGBM slots in later.
### EXECUTION track (Ben flagged it was neglected — now first-class, parallel)
- [x] OPERATING_LOOP + memory: proactivity / parallel-workstreams + overnight menu +
      EXECUTION as a first-class track.
- [x] Hands-on Alpaca paper exploration + execution doc seed (`docs/EXECUTION.md`):
      4x margin, shorting enabled, market orders queue when closed (foot-gun), etc.
- [x] Deep Alpaca API reference merged into docs/EXECUTION.md (order types/TIF,
      shorting/ETB, lifecycle+trade_updates+reconcile, wash-trade/no-long+short/BP-
      reserve foot-guns, marketable-limit cross-1-tick, EOD LOC flatten, paper-vs-live,
      L/S basket design, 16-row stress-test matrix).
- [ ] AUDIT executor for fields Alpaca REMOVES 2026-07-06 (pattern_day_trader,
      daytrading_buying_power, daytrade_count) before live.
- [ ] Build a TRIVIAL paper strategy NOW (small long/short basket, marketable limits,
      EOD flatten) to exercise signal→order→fill→reconcile end-to-end — not gated on data.
- [ ] Execution stress tests vs paper (order types, partials, cancel races, rate-limit
      backoff, shortability fails, market-closed); market-open scenarios at RTH.

### PIPELINE track (critic #5 wiring guidance logged in JOURNAL — do at panel rebuild)
- [ ] **Wire per-date demean + v1.1.0 13-feature set, then rebuild panel over ALL 51
      dates (background)**. Heed critic #5: labels need set_version/recompute-per-version
      (training_data joins on symbol,ts only); per-date outer loop; NW lag = overlap in
      timestamps; LightGBM native-NaN (don't fill); don't bump FEATURE_SET_VERSION
      constant until rebuild proven (resets live replay-equivalence). Panel = 1 date now.
- [ ] Plug LightGBM into the harness; run on the rebuilt panel (only report IC after
      the shuffle canary is green AND the t-stat is deflated).
- [ ] KNOWN-OPEN (don't bury): residual survivorship (delisted names absent); Phase-1
      parity gate never formally passed on a settled day; nightly auto-validate unwired.
- [ ] Automate nightly validate-bars on the prior SETTLED day in the scheduler
      (closes the Phase-1 parity gate honestly; needs ≥1 settled stream day — earliest
      tomorrow).
- [ ] **Then** build features+labels on source='backfill' over the corrected panel.
- [ ] Modeling harness (gate doneness on synthetic fixtures + shuffle-label canary,
      NOT on the thin/lopsided real panel).
- [x] asset_metadata (Alpaca exchange + shortable/borrow/fractionable flags),
      refreshed daily by scheduler. Universe: 939/1000 shortable — short leg must
      filter to shortable (wire into Phase 4 portfolio construction).
- [ ] Collect more supporting data (corporate actions/splits for adjustment,
      earnings calendar, GICS sector maps — sector needs a non-Alpaca source, e.g.
      the existing FMP key) — idle-time info-gathering.
- [ ] Then Phase 3: walk-forward LightGBM + honest backtest (first edge kill-gate)
      — only once the panel has real time depth.

## Current status

Fresh build started. Repo scaffolded. Design captured in `ARCHITECTURE.md`.

### Done
- Repo structure + git init.
- `ARCHITECTURE.md` (committed design, source of truth).
- TimescaleDB schema (`db/init/01_schema.sql`) — verified: 14 tables, 7 hypertables.
- `docker-compose.yml` with TimescaleDB + dashboard + Prometheus + Grafana.
- `.env` with Alpaca **paper** keys (gitignored); `.env.example` committed.
- **Dashboard** (`services/dashboard`) live on the LAN at http://192.168.1.32:8088 —
  renders STATE.md/JOURNAL.md progress + live DB health, auto-refresh 30s. This is
  how Ben monitors (no Discord/Telegram/tunnel; Claude reads DB directly in-session).

- **Ingestor** (`services/ingestor`) live: Alpaca **SIP** websocket → `bars_1m`,
  10 liquid symbols, source='stream'. Verified end-to-end (SIP→DB→dashboard).
  SIP entitlement (Algo Trader Plus) confirmed active on the account.
- **Legacy Edgar Docker stack torn down** (`docker compose down`, volumes kept);
  only the `quant` stack runs now, freeing the single Alpaca data websocket.

- **Executor** (`services/executor`) live: places one tiny paper order/day,
  records order+fill, and reconciles DB positions vs Alpaca /positions every 5 min.
  Verified: 1-share SPY filled @730.52, recorded; reconciliation works and flagged
  a stray DLTR(1) paper position left over from the old Edgar system.

- **Paper account reset to clean baseline** (Ben approved): all positions flattened,
  open orders cancelled, test order/fill/recon rows truncated. DB and broker now
  agree from zero; executor re-establishes a consistent daily order each cycle.
- **Scheduler** (`services/scheduler`) live: computes per-symbol coverage
  (received vs expected 1-min bars) for the current/last session into
  data_quality_daily; dashboard shows a Coverage panel. (First partial day reads
  low % because we started mid-session; full days from start onward read ~100%.)

### Phase 0 status: all 7 services built, healthy, and survive teardown/restart.
- Reboot survival: Docker enabled on boot + `restart: unless-stopped` on every
  service; full `compose down && up` verified — data persisted (bars 110→120, no
  loss), all services returned healthy. Prom/Grafana data moved to named volumes
  (bind-mount permission fix). Executor made idempotent across DB resets (broker
  is source of truth for "ordered today") and order errors no longer crash-loop.
- Remaining for the gate: accumulate ~5 clean trading days of coverage. A true
  host-reboot test can be run anytime (low risk) — say the word.

### Phase 1: in progress
- [x] **Shared `quantlib` aggregation library** (the parity cornerstone): per-minute
      trade & quote aggregates, pure/deterministic, with a live-vs-batch parity
      test (`make test`, 5 passing). Both ingestor and (future) backfiller call it.
- [x] **Ingestor extended** to trades + quotes via quantlib → trade_agg_1m,
      quote_agg_1m, trades_raw (30-day rolling). Verified live: realistic signed
      volume, ~1-4 bps spreads, ~11k raw trades/min across 10 symbols.
- [x] **docs/RESEARCH.md** — 40-item ML-approaches backlog (rings 1–4 + methodology).
- [x] **Universe construction** in scheduler: screens ~12.7k tradable equities by
      price>$5 and ADV$>$10M, keeps the most-liquid ≤1,000 into universe_membership
      (point-in-time, per trade_date). Pure selection in quantlib.universe (tested);
      runs once/day. Spread filter is a later refinement.
- [ ] News stream → news table (lower priority; collection-now-model-later).
- [x] **Backfiller** (`services/backfiller`, run-on-demand tool) + **validate-bars**
      gate. Verified: backfilled today's 10 symbols (4,736 bars, source='backfill')
      and compared to streamed — 99.76% OHLC / 95% incl. volume; mismatches are
      benign late-corrections (±1 print). Finding logged in JOURNAL: treat backfill
      as authoritative for training; stream is for live trading.
      Run: `docker compose --profile tools run --rm backfiller backfill-bars|validate-bars`.
- [ ] Validate on a fully-settled prior day for the official ≥99.9% gate number.
- [x] **Backfill trade/quote AGGREGATES through quantlib + validate** (parity on
      real data). Verified: trade_agg 95.2% within 2% (mean rel diff 0.7%),
      quote_agg spread 100% match, over 63 overlapping minutes. Same quantlib code
      on live ticks vs historical REST ticks → matching microstructure features.
      Run: `... backfiller backfill-aggs` / `validate-aggs`.
- [x] **Scaled live bar ingestion to the full universe**: ingestor loads ~1,000
      symbols from universe_membership and streams bars for all; trades/quotes stay
      on the liquid 10-symbol subset (TRADE_QUOTE_SYMBOLS) to keep one process
      healthy and bound raw-tick volume. (Sharded/batched ingestion for full-universe
      trades/quotes is a later optimization.)
- [ ] Watch capture rate / latency at full bar load over a day (coverage panel).
- [ ] 6-year historical backfill across the universe (disk now available; long job).
- Prereq for backfill: free SSD headroom (move recovered files off — task #3,
  awaiting Ben's OK to wipe sdb).

## Known constraints / decisions
- Deploy target: this Intel box. TimescaleDB host port **5433**, Grafana **3001**,
  Prometheus **9091** — chosen to avoid colliding with the legacy Edgar stack still
  running on the default ports.
- **Single Alpaca data websocket per account:** before the new ingestor streams,
  the legacy Edgar streamer must be stopped or it will contend for the connection.
- SSD currently ~85% full (file-recovery side task). Phase 1's 6yr backfill needs
  the recovered data moved off first — see `reference-disk-and-recovery` memory.

## Open items needing Ben
- Alert channel: Discord or Telegram? (for daily reports + alerts)
- Remote status access: Cloudflare Tunnel or Tailscale? (so Claude can read
  `/status.json` from any session)
- Approve Algo Trader Plus (~$99/mo SIP) when Phase 1 begins.
