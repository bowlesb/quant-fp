# Parity-validation current-state report + plan — 2026-06-17 ~00:30 ET

Standing feature-validation agent. Charter: prove live (`source=stream`) == backfill (`source=backfill`)
cell-for-cell across all 610 live features × symbols × clean days, manage the per-feature trust lifecycle,
quarantine divergences. This is the GATE before the historical feature-vector ML dataset can be materialized.

## Current state (verified against DB + store)

### Live feature set
- Registry: **610 features** (confirmed via `REGISTRY.feature_names()`), 35 groups.
- `feature_trust` holds **540** rows → **70 live features have ZERO trust row** (the today-deploy set).
- **0 stale** trust rows (every trust row is still a live feature) — clean diff, no retired/renamed drift.

### The 70 unvalidated new features (must validate before any model/strategy trusts them)
Grouped by owning group:
- `signed_trade_ratio` (NEW group): `signed_trade_ratio_{5,15,30,60}m`
- `trade_flow`: `signed_volume_*` (12), `trade_freq_*` (11), `tick_signed_volume_1m`, `trade_rate_accel_1m`
- `quote_spread`: `quote_imbalance_*` (11), `spread_bps_*` (11), `book_depth_1m`
- `liquidity`: `kyle_lambda_*` (5), `amihud_illiq_*` (5), `roll_spread_*` (5)
- `microstructure_burst`: `active_seconds_1m`, `peak_trades_per_second_1m`, `inter_arrival_cv_1m`
- `momentum_run` (v2.0.0 rewrite): `residual_skew_*`, `max_runup_1m`, `max_signed_run_1m`, `signed_run_count_1m`
These are overwhelmingly ORDER-FLOW / MICROSTRUCTURE features → they depend on raw **trades + quotes**, so
parity for them is only meaningful where raw quote tape exists (4001 symbols/day, see below).

### Prior validation coverage = effectively ONE thin evidence run
- `feature_validation_day`: **1 day** only (2026-06-15), 540 features.
- `stream_symbol_day_cleanliness`: **20 symbols** for 2026-06-15, of which only **3 clean**.
- `3 clean < MIN_CLEAN_SYMBOLS (20)` → that day is **too contaminated to grade**. The legacy
  `feature_trust` grades from it (335 "divergent", 84 "A") are NOISE off 3 marginal symbols, not real.
- `feature_trust.lifecycle_state` is **NULL for all 540** → `trust_lifecycle` (the contamination-aware
  state machine) has **never run**. `feature_parity_defect` = 0 means BARELY-CHECKED, not clean.
- Net: **no feature has earned trust.** Validation has essentially not been run at production scale.

### Store coverage (what is validatable right now)
Stream capture only started recently — only two stream-feature days exist:
- **2026-06-16**: 2836 stream symbols, raw tape present (7682 bars / 7671 trades / 4001 quotes). SETTLED.
- **2026-06-15**: 10381 stream symbols, raw tape present (same raw breadth). SETTLED.
- No earlier stream days. (The roadmap's "610 × 18 months" is a FUTURE materialization; today parity can
  only grow over these 2 captured days. The raw tape itself goes back to ~March 2026 — 7682 symbols.)

## Plan (priority order)

1. **NOW — full sweep 2026-06-16** (`DAY=2026-06-16 ops/validation_sweep.sh`, chunk 200): materialize
   backfill for all 2836 discovered stream symbols from `/store/raw`, validate cell-for-cell, write the
   contamination-aware lifecycle. This is the most recent settled day with the new features live →
   first real parity read on the full 610 set.
2. **Immediate regression check** — `parity_audit 2026-06-16 90` on ~90 liquid names, focused on the 6
   new-feature groups. Fast independent confirmation; FLAG any DIVERGE in the new features as a deploy
   regression before the slow sweep finishes.
3. **Second clean day — sweep 2026-06-15** (the 10381-symbol day) so features get a SECOND clean day →
   `MIN_CLEAN_DAYS=2` is reachable → features can move PENDING → VALIDATED.
4. **Grade + quarantine**: trust_lifecycle grades over CLEAN RTH symbol-days only; DIVERGENT features get
   a kept+flagged `feature_parity_defect` row. Known UBER/DIS degenerate-fit divergences
   (trend_quality/clean_momentum/idio_vol, PR #34) classified as documented known-limitations, not new.
5. **Daily going forward**: `ops/daily_lifecycle.sh` (acquire raw T+1 → sweep) keeps coverage current.
6. **Downstream gating**: `trust_lifecycle.trusted_feature_names()` / `feature_trust_grades()` already
   expose the trust state for the bus / Modelling Agent to gate on.

## Tooling note (fixed, for operators)
`ops/sandbox.sh` passes `-e DB_PASSWORD="${DB_PASSWORD:-mock}"` which OVERRIDES the `.env` value with
`mock` unless `DB_PASSWORD` is exported in the calling shell → DB auth fails from sandbox. Workaround in
use: `export DB_PASSWORD=$(grep ^DB_PASSWORD= .env | cut -d= -f2-)` before sandbox calls. Candidate
tooling fix (PR): have sandbox.sh source `.env`'s DB_PASSWORD as the default instead of `mock`.

## Milestone update — findings from the first real validation pass

### NO DEPLOY REGRESSION in the 70 new features (the headline)
`parity_audit 2026-06-16 90` (real RTH data, ~90 liquid names) over ALL 610 features:
`MATCH=571 NEEDS_DATA=39 TOTAL DIVERGENCES: 0`, on BOTH the `compute_latest` and the
`IncrementalEngine.step` (live production) paths. The order-flow groups that own the 70 new features
(trade_flow / quote_spread / liquidity / signed_trade_ratio / tick_runlength / microstructure_burst)
MATCH backfill cell-for-cell on dense RTH data. The deploy is parity-clean at the compute level.

### Two tooling GATES found + fixed (the sweep literally could not validate the new features)
1. `materialize_from_raw` reads only `/store/raw/bars` -> produced backfill for the 30 bar groups but
   NONE for the 6 tick/quote groups -> `validate()` raised "no group had settled backfill". FIX:
   `materialize_from_raw_full` (tick-enriched minute_agg + trades from `/store/raw/trades`+`quotes`),
   wired into the sweep (default on). All 6 order-flow groups now write a backfill side (verified on
   2026-06-15: trade_flow/quote_spread/liquidity/signed_trade_ratio/tick_runlength/microstructure_burst
   all present).
2. Once the distributional groups (tick_runlength/microstructure_burst) fed feature_day for the first
   time, two `pl.concat` sites raised `Int64 vs UInt32 ... n_compared`. FIX: cast count columns to Int64
   before concat (no value change). Unit-tested. (PR `feature/parity-tick-materialize`.)

### Why 06-15 still cannot TRUST-validate the 70 new features (honest coverage limit, NOT a bug)
The live capture for the order-flow features on 2026-06-15 ran POST-MARKET ONLY: every stored stream
cell for `signed_volume_5m` (and the others) is at 20:49–23:58 UTC (16:49–19:58 ET), ZERO inside RTH.
The sweep grades RTH only -> n_compared=0 -> grade U (correctly "not enough data to validate"), with
`n_mismatch=0` everywhere (NO divergence). On the post-market overlap the windowed features do differ
(signed_volume_5m 64% match, worst rel 30x) — the documented sparse/restart windowed-divergence the
cleanliness machinery is built to EXCLUDE; it is outside the RTH grading scope and matches the live
cold-buffer regime, not a parity defect. `signed_trade_ratio` has NO stream partition on 06-15 (genuinely
new today). VERDICT: the 70 new features need a CLEAN FULL-RTH day (a day where live capture ran the whole
session) to earn trust. parity_audit already proves the compute paths agree on RTH data, so this is a
data-coverage wait, not a regression.

### Data note
2026-06-16 raw tape is INCOMPLETELY acquired (most symbols had empty bar parquets; `quant-backfill` is
still acquiring 18mo of tape) -> 06-16 not yet sweepable; 06-15 raw tape IS complete and was used.

## Status / next
- Tooling fixes committed + pushed: branch `feature/parity-tick-materialize` (PR pending review).
- Next: run the FULL tick-aware `sweep_day` on 2026-06-15 (all discovered symbols) to grade the 540
  bar-derived features over a real clean day (start them earning trust); then validate the 70 new
  features on the next CLEAN FULL-RTH day once 06-16/06-17 raw tape lands.
- Standing: daily `ops/daily_lifecycle.sh` going forward; quarantine any real RTH divergence to
  `feature_parity_defect` (none found so far).
