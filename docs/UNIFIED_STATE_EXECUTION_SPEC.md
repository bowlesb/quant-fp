# Unified State-Execution Spec — speed correct by construction

> Status: SPEC for gate-read (2026-06-20). Author: Latency. Supersedes the "monitor + shave bespoke
> per-group ms" posture. North star: every group's live vector is `A(cached) + B(incremental)` —
> compute-once-per-day for the intraday-invariant class, seed/fold/emit for the stateful class — so a
> fast vector is a PROPERTY OF THE DECLARATION, not a hand-tuned `compute_latest`. Grounds the
> generalization in docs/STATE_ABSTRACTION.md (the FeatureState interface + the `seed(H);fold(m)==seed(H+m)`
> parity invariant) and the working kinds in incremental.py / stateful.py / declarative.py.

## 1. The two classes (every one of the 63 groups is exactly one)

**CLASS A — intraday-invariant** → compute ONCE per session, cache, broadcast every minute (≈0 work/min).
  - A1 (static snapshot): output is a pure function of a per-session-constant frame (`reference` /
    `daily` snapshot), broadcast to (symbol, minute). Recompute-per-minute = pure waste.
  - A2 (timestamp-deterministic): output is a closed-form function of the `minute` TIMESTAMP only (no
    buffer scan, no per-symbol price/volume) — same value for every symbol at a given minute. Cheap, but
    still recomputes the ET conversion per minute; a per-minute-keyed cache makes it ≈0.

**CLASS B — prior-state + tiny per-minute compute** → seed once, `fold(new_minute)` in O(state), emit =
read state. The windowed/cumulative/extrema/EMA/lag/regressor/tick KINDS from STATE_ABSTRACTION.md §"state KINDS".

The execution engine, not the group, owns the A-cache and the B-fold/emit. A group DECLARES its class +
(for B) its kind(s) and writes `emit()` once. The `compute()` (backfill rolling form) remains the parity
oracle; the engine guarantees the live state equals the backfill state (`seed(H);fold(m)==seed(H+m)`).

## 2. Classification of all 63 groups (P0 deliverable — the audit)

Derived from base class + data-dependency signature (registry introspection 2026-06-20).

### Already on the abstraction (the reference implementations)
- **23 ReductionGroups / 377 feats** (volume, volatility, momentum, trade_flow, price_volume, OLS groups …):
  KIND = additive-window (`WindowedSumState`). PARITY-TRUE in batch; **FP_INCREMENTAL gated OFF** on the
  ~9 cancellation-prone groups (Σx²−(Σx)²/n) → live falls back to the rolling recompute. **= P2 target.**
- **4 StatefulGroups / 87 feats**: `technical` (EMAState), `candlestick` + `price_returns` (LastKState),
  `price_levels` (ExtremaState). KIND-B working today. **= the P3 reference pattern.**

### CLASS A — intraday-invariant (the P1 target; recompute-every-minute is pure waste today)
| group | feats | A-type | today |
|---|---|---|---|
| `asset_flags` | 4 | A1 (reference only) | recomputes/min, no cache |
| `sector` | 12 | A1 (reference sector map) | recomputes/min, no cache |
| `calendar` | 4 | A2 (minute timestamp) | recomputes/min |
| `calendar_events` | 7 | A2 (minute timestamp) | recomputes/min |
| `prior_day` | 10 | A1 (daily snapshot) | ALREADY `_daily_cache` ✓ (the pattern to generalize) |
| `multi_day_returns` | 28 | A1 (daily) | daily-broadcast |
| `multi_day_vwap` | 10 | A1 (daily) | `_daily_cache` ✓ |
| `overnight_intraday_split` | 3 | A1 (daily) | `_daily_cache` ✓ |
| `daily_beta` | 3 | A1 (daily OLS) | `_daily_cache` ✓ (#238) |
| `overnight_beta` | 3 | A1 (daily OLS) | `_daily_cache` ✓ (#262) |
| `liquidity_rank` | 2 | A1 (daily + universe) | `_daily_cache` ✓ (#264) |
| `return_dispersion` | 10 | A1 (daily) | `_daily_cache` ✓ |
| `edgar_filing_frequency` | 10 | A1 (filings snapshot) | per-session cache |

A-total ≈ 106 feats across 13 groups. **8 already cache via the bespoke `_daily_cache`; 5 (asset_flags,
sector, calendar, calendar_events, + audit edgar) do NOT and recompute every minute.** P1 = unify these
under ONE engine-owned A-cache (the `daily_snapshot_token` + a minute-timestamp key for A2), retiring the
per-group `_daily_cache` copies.

### CLASS B — bespoke per-minute today, must migrate to a declared kind (P3/P4)
| group | feats | kind it should declare |
|---|---|---|
| `runner_state` / `dumper_state` | 6+6 | cumulative (session max/min/sum since open) |
| `gap_fill_state` | 2 | cumulative (session-open anchor) |
| `intraday_seasonality` | 2 | cumulative (running since-open mean) |
| `momentum_run` | 12 | additive-window + lag (rolling + run-length) |
| `swing` | 9 | extrema / lag (zigzag pivots) |
| `draw_range` | 3 | extrema (running cum-max/min excursion) |
| `cross_sectional_rank` / `peer_relative` / `breadth` | 6+3+30 | GATHER (universe at-T, runs once in the reader phase) |
| `market_context` / `market_turbulence` / `sector_beta` / `sector_return` | 36+5+6+8 | GATHER (universe broadcast / rolling OLS) |
| trades-frame groups (inter_arrival, large_print_burst, microstructure_burst, print_hhi, size_entropy, subminute_gap_fano, tick_runlength, trade_size_dist, signed/ratio) | ~24 | tick-ring (Layer C) |

NOTE: the GATHER groups already run ONCE in the reader gather phase (not per-shard) — they are NOT a
per-bet cost (gather_emit ≈7ms/shard measured). They stay gather; not a B-fold target.

## 3. seed / fold / emit as the DEFAULT path (the architecture)

Today the emit loop (stream_sim `_minute`) already has: reduction unified-emit (additive state), stateful
shared-fold (EMA/lag/extrema), gather-once, and a `other_groups` loop that calls each remaining group's
**bespoke `compute_latest`**. The unified design REPLACES the bespoke loop with class-routed execution:

```
for group in groups:
    if group.feature_class == A:  out = A_CACHE.get_or_compute(group, session_token)   # once/session
    elif group.feature_class == B: out = group.state.emit()    # after engine seed/fold
    # GATHER + trades-ring keep their existing once-per-reader / tick-ring paths
```

- `feature_class` + (for B) `state_spec()` become DECLARED on the group (like `inputs`/`declare()`).
- The engine owns: the A-cache (session-token keyed), the B seed/fold/emit driver, and — critically — the
  BACKFILL dual-form generator so `compute()` (the rolling oracle) stays the parity truth. A group author
  writes `emit()` ONCE; never the fast/slow split.
- PARITY GATE per kind: `seed(H); fold(m) == seed(H+m)` cell-for-cell (the existing test_fp_incremental /
  test_fp_stateful invariant, extended to each migrated kind). A group cannot enter class B until its
  kind passes this on real data in the validation ledger. Until then it stays on `compute()` (safe slow path).

## 4. Rust-resident state (P3, sequential-hot)

For the sequential-hot kinds (cumulative session-state, run-length, the swing_dc_fold pattern), the fold
is O(1)/minute but Python-call-overhead-bound across 462 syms × ~50 groups. Generalize the existing
Rust-resident fold (the `swing_dc_fold` / kernel pattern) so the per-minute fold runs in-kernel over the
shared coded buffer — one Rust call advances ALL same-kind groups' state, emit reads the numpy view. This
is the lever that takes per-bet compute from ~305ms toward the <100ms floor (the 279ms per-group-compute
floor IS the Python-per-group-frame model this removes).

## 5. Phasing (gated, sequential — this is a project)

- **P0 (this doc):** the spec + the A/B classification above. GATE-READ with Lead. ✅ deliverable.
- **P1 (next, concrete + value-identical):** the engine-owned A-cache. (a) Unify the 8 bespoke `_daily_cache`
  groups under ONE `SessionCache` keyed by `daily_snapshot_token`; (b) add the 5 uncached A groups
  (asset_flags, sector, calendar, calendar_events, +edgar audit). Each value-identical (byte-eq
  test_fp_latest) + fp-NEUTRAL. EXPECTED: removes ~106-feat recompute/min from the per-bet path; the
  measured win sizes the A-class. *Caveat from the L1 measurement: prove each with a before/after bench —
  vectorized polars is cheap, so an A-cache only wins where the per-minute recompute is genuinely heavy
  (the daily-OLS groups: yes; the trivial calendar closed-forms: likely marginal — measure, don't assume.)*
- **P2:** stable-summation (Welford/Kahan) for the ~9 cancellation-prone reduction groups → FP_INCREMENTAL
  parity-true → enable incremental as the DEFAULT for the 23 reductions. (Lead sequences the fp/default flip.)
- **P3:** generalize Rust-resident fold for the sequential-hot B kinds (cumulative/run-length/swing).
- **P4:** delete the bespoke `compute_latest`; adding a feature = declaring A or B = fast by construction.

## 6. Measured grounding (why this, not more bespoke shaving)

- Per-bet bar→vector (single 462-sym shard, isolated): ~461ms p50; per-shard compute ~305ms (other_emit
  213ms = the per-group Python frame-build loop), the rest IPC/contention. Per-group SUM across all 63 =
  ~531ms — already mined; no group >70ms after the 9 wins.
- Shard-count sweep @7400 (Lever 3): 8-shard p50 ~1475ms, 12-shard ~1458ms → FLAT above 8 shards. The
  e2e bottleneck is NOT shard parallelism; it's per-shard compute + IPC. (16/24 confirming.)
- CONCLUSION: bespoke per-group shaving is exhausted; the 279-305ms per-shard-compute floor is the
  per-group-Python-frame model itself. Only the abstraction (A-cache eliminates A-recompute; B-fold +
  Rust-resident eliminates the per-group frame-build) crosses toward <100ms. One investment, both wins
  (latency + fast-by-declaration cheap features).
