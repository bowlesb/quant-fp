# Sub-50ms in production — measurement + safety regime

> Status: ACTIVE (2026-06-14). Workstream #1. The honest target, exactly how we'll know we hit it, and
> how we guarantee a large architectural change (incremental V2, continuous-flow harness) did not buy
> speed by breaking parity. Speed that breaks parity is a FAILURE, not a win — that rule is binding.

## 1. The target, stated precisely

**Bet-latency ≤ 500ms; vector-compute < 100ms (Ben's stated goal, 2026-06-18).** Bet-latency is the
wall-clock from a ticker's bar arriving to THAT ticker's feature vector being ready to act on (per the
bet-latency-metric rule — not the slowest-shard p99). Vector-compute (< 100ms) is the per-shard minute-mark
work that turns the just-arrived minute into the feature matrix.

**Prior internal target (still the engineering stretch): p99 per-minute compute < 50ms for 519 features ×
10,000 tickers, steady-state, on the 32-core box.** Stretch: < 30ms. Floor we must not regress past: the
current ~617ms 8-shard compute. The incremental fast path already clears < 100ms in sim (§6); the open
gap is that it is NOT YET ENABLED in production (`FP_INCREMENTAL` unset live — the live path runs the
batch O(window) recompute at ~1.75s p50). Enabling it safely, per-group, is this cycle's deliverable.

"Per-minute compute" = the work that MUST happen at the minute mark to turn the just-arrived minute
into the full feature matrix. It does NOT include the parquet write (that happens AFTER the bet is
placed — established earlier; we measure compute and write separately and only gate on compute).

## 2. How we measure success (no moving goalposts)

Every claim must satisfy ALL of these, or it doesn't count:

1. **Real scale.** 10,000 symbols × the full registered feature set (~519), 8 shards × 4 threads
   (the tuned 10k/32-core layout). Not 1250×60 except as a unit-level A/B.
2. **Steady state.** Measure only on post-warmup minutes with FULL buffers — the first ~`max_window`
   minutes are excluded (cold buffers aren't the production regime). Report how many minutes measured.
3. **Percentiles, not means.** Report p50 AND p99 over the measured minutes. The bet deadline is a
   tail problem; a good mean with a 200ms p99 fails.
4. **Continuous trades + quotes, not pre-aggregated bars.** The harness must flow real-shaped trades
   and quotes continuously and do the tick→minute aggregation at the mark, because that aggregation is
   part of the minute-mark cost. Feeding pre-aggregated minute columns understates the real number.
5. **Honest decomposition.** Every measurement reports where the time went: ingestion+aggregation /
   fold / assemble-from-sums / cross-shard gather. A single number with no breakdown is not accepted —
   we need to see the 86%-shuffling share actually fall.
6. **End-to-end sanity.** Alongside compute-only, report data-arrival → features-ready wall-clock at
   10k, so we don't optimize the compute while an ingestion bottleneck dominates.

The canonical command(s) and their output (p50/p99 + decomposition at 10k) get pasted into this doc as
the success record, with the git SHA, so the result is reproducible and dated.

## 3. The architectural changes in flight

| change | what | risk class |
|---|---|---|
| **Incremental V2 assemble** | build canonical reduction columns (means, stds, OLS stats) DIRECTLY from `WindowedSumState` running sums instead of re-deriving over the buffer — kills the 45% derive + shrinks the 27% assemble | HIGH (parity-critical: must equal backfill cell-for-cell) |
| **Continuous trades+quotes harness** | mock stream emits continuous protocol-faithful trades+quotes; bench aggregates tick→minute at the mark and measures | MEDIUM (measurement fidelity; must not change feature values vs the bar path) |

## 4. How we catch issues from these changes (the safety regime — binding)

A large architectural change is only allowed to land when it passes EVERY gate below. This is the
"upfront about catching issues" contract:

1. **Parity gate (the hard one).** `tests/test_fp_incremental.py` (accumulator sums == kernel,
   cell-for-cell) AND `tests/test_fp_incremental_features.py` (incremental `step()` == `compute_latest`
   == backfill `compute`, within each feature's declared tolerance) MUST pass. If V2 diverges, we fix
   V2 — we NEVER loosen a tolerance to make a speed change pass. A divergence is the failure mode the
   whole platform exists to prevent.
2. **Full-suite regression.** All 172 tests green, every run. A latency change that breaks an unrelated
   test is not done.
3. **Production safety net = the validation ledger (#2).** This is the deepest protection and why the
   two workstreams reinforce each other: even after V2 ships, the ledger validates the data we ACTUALLY
   collected live against backfill, per cell, durably. If V2 introduces a subtle drift that the unit
   tests' synthetic data missed, the ledger surfaces it as real-data mismatches and the affected feature
   flips `divergent` — caught in production, before any model trusts it. The architectural change is
   thus gated twice: synthetic parity tests pre-merge, real-data ledger post-deploy.
4. **Drift is bounded and monitored.** Incremental float sums drift slowly (add/subtract over ~390
   minutes ≪ each feature's tolerance); we re-seed `WindowedSumState` from the buffer each session
   (also crash recovery), and the parity test proves the bound holds. The ledger's per-day trust grade
   would show drift as a slow value-rate decline before it ever crosses tolerance.
5. **Isolation + revert points.** V2 lands on `feature/incremental-engine` in an isolated worktree; it
   merges to the platform only when parity + the 10k latency number are BOTH proven. The stable
   `fp-platform` / validation-ledger work is never destabilized by the latency push.
6. **What stays on the old path is stated explicitly.** Non-windowed-sum features (cumulative OBV,
   shift(k), run-length) are NOT slice-derivable and remain on their current compute — V2 only replaces
   the declarative windowed-reduction assemble. Any feature moved to the fast path must be named, and
   its parity verified individually.

## 5. Definition of done for #1

- 10k × 519, steady-state, p99 < 50ms compute, with the decomposition showing the shuffling share
  collapsed — pasted here with SHA.
- Continuous trades+quotes flowing; tick→minute aggregation included in the measured number.
- Parity tests + full suite green; the moved features enumerated.
- The validation ledger confirms the live path (V2) reproduces backfill on a real overlay day.

Until all four hold, #1 is `in progress`, not done — and we say so.

## 6. Results record (honest, dated)

**2026-06-14 — Incremental V2 (slice-derive + stateful regressors), branch `feature/incremental-engine`
@ `a8e63de`.** PARITY-TRUE (all incremental + batch parity tests pass, no tolerances loosened; OBV
cumulative and time-axis OLS handled via a new `stateful_regressors` running-state API). NOT yet at
target.

Measured 1250×60 (per-shard production scale), `--cpus=2`, reproducible:
| stage | V1 | V2 |
|---|---|---|
| fold (derive+update+running_long) | ~67ms | ~54ms (cpu-bound) / ~35ms unconstrained |
| full step (fold + assemble) | ~97ms | **~80ms** |
| shared `assemble_from_long` | ~26ms | ~26ms (unchanged; same code batch runs) |

Decomposition verdict: the fold is no longer dominant. The remaining cost is (1) slice-derive
`over("symbol")` per-expression overhead (~0.5ms × 43 exprs, fixed per-partition not per-row) and
(2) the polars `assemble` pivot (~26ms). 10k runs as 8 parallel shards of ~1250, so per-shard ≈
wall-clock; ~80ms is the production per-minute figure, not 8×.

Caveats vs §2 criteria NOT yet satisfied: measured at `--cpus=2` not the full 32-core 8-shard layout;
1250×60 not 10k steady-state with continuous trades+quotes; single-run not p50/p99; box runs other
agents 24/7 so absolute ms vary. So this is a parity + relative-improvement result, NOT a certified
production p99. The certified 10k steady-state measurement is still owed.

Decision: V2 stays ISOLATED (not merged) — per §4.5 it merges only when parity AND <50ms both hold.
Next lever = numpy-native `emit` (the §STATE_ABSTRACTION `emit()` step): build canonical columns from
running state, bypassing the assemble pivot. De-risk via a parity-gated prototype on the hottest group
before any broad migration.

**2026-06-14 — numpy-emit prototype verdict (branch `feature/incremental-emit` @ `7211bb4`).**
PARITY-TRUE and CORRECTS the decomposition. `emit_numpy` builds every canonical column
(mean/std/sum/slope/corr/r2/mean_y, incl. OBV cumulative) directly from the numpy running sums with
character-identical algebra — proven **cell-IDENTICAL (tol=0.0)** vs the polars path and vs batch
`compute_latest`; covers all 11 reduction groups with zero per-group rollout. Measured (1250×60,
`--cpus=2`):
| stage | polars | numpy-emit |
|---|---|---|
| assemble pivot | 26.1ms | 19.5ms (**−9.5ms / 33%**) |
| full step | 84.8ms | 74.7ms (**−10ms**) |

**Corrected bottleneck:** the slice-derive `over("symbol")` fold (`_derived_row`) is **~53ms** and
dominates the step — NOT the pivot. **Driving assemble to 0 still leaves ~58ms (>50ms).** So:
- numpy-emit is the wrong lever for <50ms ALONE — but it's a free, parity-true ~10ms win: **ship it**.
- the **Rust slice-derive kernel** (replacing the ~53ms `over("symbol")` per-expression fold) is the
  NECESSARY lever. Sequence to <50ms: land the Rust slice kernel first (cuts the fold), THEN numpy-emit
  removes the pivot to clear 50ms comfortably. Both parity-gated; V2 + emit stay isolated until the
  combined path proves <50ms at the certified 10k measurement.

**2026-06-14 — Rust slice-derive kernel (branch `feature/incremental-rust` @ `0986601`). CLEARS <50ms.**
PARITY-TRUE: the kernel output == polars `_derived_row` cell-for-cell (tol=0 on finite cells, null==null),
proven across warmup (lag-1/2/3 null) AND a deliberately-introduced missing-prior-bar hole; no tolerances
loosened. Root-caused: the entire ~53ms was a `close.shift(k).over("symbol")` re-partitioning the
1250-symbol slice — the ONLY grouped op in the derive (verified by serializing every expr). The kernel
does one ordered pass emitting lag-1..k per symbol; a `lag_specs` guard raises loudly if a future group
adds a different `over("symbol")` op (can't silently break parity).
| stage | before | after |
|---|---|---|
| slice-derive | 53.9ms | **2.5ms (~20×)** |
| full step (Rust derive + numpy-emit) | 84.8ms | **~23ms** |

10k/8-shard: per-shard step ~23ms, shards parallel → ~23ms wall-clock for 10k — clears <50ms with wide
headroom, far inside the <100ms bar and the ≤300ms acceptance. The fold/emit (~20ms) is now the larger
share; a Rust assemble is the next lever IF a sub-15ms step is ever wanted (not needed for the bar).

**Convergence (in flight, branch `feature/streaming-sim`):** merge the fast path + the full-flow mock +
the tick-aggregation consumer; wire the incremental engine + `tick_capture` into the capture loop; run
the 10k sim on continuous trades+quotes+bars; measure p50/p99. The compute step is proven ~23ms; this
measures it IN the real flow (tick-agg + fold + emit + ingestion) to certify the <100ms bar end-to-end.

**2026-06-18 — production-enablement parity audit + per-group gate (branch `inc-latency`).** The fast
path is proven in sim but NOT enabled live (`FP_INCREMENTAL` unset → live runs the batch O(window)
recompute at ~1.75s p50). Audited which ReductionGroups can be turned on safely by replaying a realistic
fluctuating-membership minute stream through BOTH paths and tabling the per-group batch-vs-incremental
divergence (375-symbol shard, whole-buffer derive):

| regime | divergent groups (worst feature) | clean groups |
|---|---|---|
| realistic (vol≈0.02) | `volume` 28x (volume_zscore_3m), `price_volume` 34x (pv_correlation_3m) | the other 15 ≤ 2.2x |
| smooth near-perfect-fit (vol≈0.002) | + `trend_quality` 254x (price_r2_5m), `clean_momentum` 96x | the other 13 ≤ 1.8x |

The "x tolerance" is a near-degenerate-cell artifact: the WORST absolute divergence is **~2e-8** (a
perfect-fit corr==1.0, an n=2 z-score==1/√2) — the float floor, not a value bug. But it crosses the 10x
parity-self-check breach ratio, so per the binding rule (§4.1: never loosen a tolerance; a divergent
group must not be the written source) those 4 stay on batch. ROOT CAUSE is the same for all four:
`_canonical`/`_ols_stat_exprs` build variance/cov as `Σx²−(Σx)²/n` (a difference of large near-equal
sums), and the incremental running add/subtract sums round differently from the batch fresh window sums;
the cancellation amplifies that at large-magnitude (raw share volume) or near-perfect-fit cells.
`market_beta` (also OLS) is CLEAN (0.000x) because it regresses well-conditioned returns.

**Mechanism delivered:** a declarative per-group gate — `ReductionGroup.incremental_safe` (default True).
`process_bars` now SPLITS each reduce bucket: `incremental_safe` groups assemble from the running sums
(`step`), the 4 sensitive groups keep the batch fresh-sum recompute even under `FP_INCREMENTAL`. So
flipping `FP_INCREMENTAL=1` serves 13 groups (≈ all the bar+tick reductions) from the fast path and
leaves the 4 byte-identical to batch — parity-true by construction, no bus-fingerprint change. Pinned by
`tests/test_fp_incremental_capture.py` (gated groups byte-identical to batch on the smooth walk; no breach
across the full 30-group set; the conditioning divergence still bites the engine directly — the gate is
load-bearing). Sandbox latency (375-symbol reduction-bucket pass): batch p50 ~127ms → incremental p50
~77ms (1.64x); per the sim §6 the full fast path is ~88ms p99.

**Production rollout (staged, per the verification-culture no-big-bang rule):**
1. Stage with `FP_INCREMENTAL=1 FP_INCREMENTAL_PARITY=1` live for ≥1 session — runs BOTH paths, writes
   the BATCH truth, records the per-bucket divergence to Prometheus (`record_incremental_parity`). Watch
   for any breach beyond the 4 expected-gated groups (there should be none — the gate excludes them from
   the compared set). This is the live evidence gate before trusting the fast path as the source.
2. Then `FP_INCREMENTAL=1` (drop PARITY): the 13 safe groups become the written source from the running
   sums; the 4 stay batch. The validation-ledger nightly sweep (§4.3) confirms the live fast-path output
   reproduces backfill per cell on the overlay day.
3. FOLLOW-UP to flip the 4: give the OLS/variance family a centered (Welford/co-moment) incremental form
   so `Σx²−(Σx)²/n` is replaced by a running centered M2 (and the OLS residual-SS by centered co-moments)
   — exactly the stabilization `residual_analysis`/`momentum_run` already use (window-local centered fits).
   Compensated (Neumaier) running sums help (~5x) but do NOT close the cancellation alone; the centered
   form is the real fix. Until then the 4 ride batch (correctness > the last 4 groups' speed).

NOTE: `momentum_run` and `residual_analysis` (the heaviest live groups, ~497ms / ~247ms per the live
per-group panel) are hand-written `FeatureGroup`s, NOT `ReductionGroup`s — the incremental engine does
not touch them, so `FP_INCREMENTAL` does not reduce their cost. Their speed lever is the stateful-engine /
Rust-kernel migration (separate workstream), not this gate.

## 7. Deferred <100ms roadmap (written 2026-06-18; DEFERRED, not abandoned)

> Decision (team lead, 2026-06-18): STAND DOWN on latency execution. Latency is production-readiness,
> not edge-blocking — we sit at ~1.75s p50, comfortably inside the 60s minute-bar cadence, and sub-second
> only matters once an edge needs to react fast (the active priority is the order-flow trust jump + edge
> hunt, not speed). The two remaining levers below are scoped and ready to pick up the moment latency gets
> the green light; nothing here is built yet. `FP_INCREMENTAL` stays OFF in production until the team lead
> sequences the staged rollout in §6.

Where we are: the 13 safe reduction groups already clear <100ms on the incremental fast path in sim (§6,
~88ms p99) and the gate to enable them is merged (PR #117) but NOT turned on. The remaining gap between
"fast path proven in sim" and "live <100ms" is two levers, in priority order.

### Lever #1 (SMALL, low priority) — stable incremental form for the 4 gated reduction groups

> **STATUS 2026-06-19 [Latency] — 3 of 4 SHIPPED (PR: lat/inc-stable-n2-guard), Lead-deploy + re-trust owed.**
> The n==2 perfect-fit value-correcting guard (`_OLS_PERFECT_FIT_COUNT`: emit `r2=1.0` / `corr=sign(cov)` at
> `b==2`) is implemented in ALL THREE twins (`_ols_stat_exprs` polars, `_ols_stat_numpy`, the rust
> `assemble_canonical`). It closes the b==2 corner cell-for-cell, so with the origin-rebase (#132) already in,
> **`trend_quality`, `clean_momentum`, and `price_volume` (pv_correlation) are now parity-true** on smooth /
> degenerate (n==2, sparse) / control walks (verified: worst pv_correlation absdiff ~7e-15, all flipped groups
> well under the 10x breach on both slice-derive and whole-buffer). They are flipped `incremental_safe = True`
> (versions bumped: trend_quality 1.0.0→1.1.0, clean_momentum 1.0.0→1.1.0, price_volume 1.1.0→1.2.0). The guard
> changes the SHARED batch algebra at b==2 cells (max Δ ~1.7e-4, float-noise→exactly-1.0), so it is a
> **fingerprint change → needs a Lead-coordinated deploy + re-trust of those 3 groups** (crypto 24/7 enables the
> re-trust). Measured on a 375-sym shard: the 3 flipped groups' batch `compute_latest` p50 254ms / p99 322ms →
> incremental `step` p50 24.5ms / p99 29.4ms — **~10.4x p50 / ~10.9x p99**, removing ~230ms p50 / ~293ms p99 of
> batch reduction work (price_volume 163ms, clean_momentum 48ms, trend_quality 34ms p50 each, gone from batch).
> **`volume` REMAINS GATED** — its blocker is the variance-family std (power-sum `sqrt(Σv²−(Σv)²/n)` live vs
> backfill `rolling_std_by`), a batch-vs-canonical FORMULA gap (verified present even at zero incremental drift:
> null/non-null flip at the std floor on near-constant huge volume + ~7e-4 at the n=2/3 z-score). Its parity-true
> fix is the centered power-sum std in the SHARED batch kernel — Lead-owned, invasive — and is the only Lever-1
> remainder. `tests/test_fp_incremental_capture.py` extended: per-group cell-for-cell parity on smooth+degenerate
> walks for the 3 flipped groups (the old xfail is now an asserting parity test), plus a load-bearing
> volume-still-breaches test.

**Scope:** flip `volume` (volume_zscore), `price_volume` (pv_correlation), `trend_quality` (price_r2),
`clean_momentum` from `incremental_safe = False` back to `True` by removing the batch-vs-incremental
conditioning divergence (§6 follow-up #3). All four diverge through the SAME mechanism: variance/cov built
as `Σx²−(Σx)²/n` (a difference of large near-equal sums) on large-magnitude values (raw share volume) or a
near-perfect fit (price_r2≈1), where the incremental running add/subtract sums round differently from the
batch fresh window sums. Worst ABSOLUTE divergence is ~2e-8 (float floor) — this is a parity-self-check
nicety, not a value bug.

**What it takes (all contained to `quantlib/features/incremental.py`; batch path untouched; per-group
parity-gated; reversible via `incremental_safe`):**
- *OLS family — time-axis origin (price_r2, clean_momentum).* **SHIPPED 2026-06-18 (PR lat-inc-stable),
  engine-only, no batch change.** The engine's `time` StatefulRegressor used a FIXED seed origin (`ref_epoch`)
  that GROWS all session, so the OLS x-sums were large and rounded the `cov²/(var_x·var_y)` cancellation
  differently from the batch's per-frame centering. FIX: `IncrementalEngine` now ROLLS `ref_epoch` forward
  each fold (`_roll_time_origin` + `WindowedSumState.rebase_time_axis`, the exact affine `x→x−Δ` transform on
  the running OLS sums) so x stays pinned at `_TIME_ORIGIN_LAG`. This zeroes the divergence for every n≥3
  cell and bounds x over a full session (verified: price_r2_5m worst tol-ratio stays ~0.02x from minute 30 to
  350+).
- *The REMAINING flag-flip blocker is NOT the origin — it is the n==2 perfect-fit corner.* MEASURED this
  cycle: with the origin-rebase in, the only residual breach on the flagged smooth-churn walk
  (`test_ols_near_perfect_fit_is_flagged`) is at **paired count b==2** — two points define a line exactly, so
  `dy = b·Σyy − (Σy)²` cancels to float noise and r2 is computed as `noise/noise ≈ 1.0±ε`; the two paths'
  noise differs (price_r2_5m 254x, clean_momentum_score_5m 96x — both ENTIRELY the b==2 cells). A shared,
  value-CORRECTING guard closes it: at b==2 emit `r2 = 1.0`, `corr = sign(cov)` in BOTH `_ols_stat_exprs`
  and `_ols_stat_numpy` (an OLS line through two points is a perfect fit; 1.0 is the true value, the current
  0.9998 is float noise). VERIFIED: origin-rebase + n==2 guard takes ALL FOUR groups to ≤0.03x (clean), and
  the guard changes ONLY b==2 cells (93 cells, max Δ 1.7e-4, float-noise→exactly-1.0; zero n≥3 cells touched).
  **This guard touches the SHARED batch algebra → it changes backfill feature values at the degenerate cells →
  it needs a per-group VERSION BUMP on the 4 groups → fingerprint-coordinated. That is the LEAD's to sequence
  (Latency proposes; the n==2 guard + version bumps + the flag flip ride together in a Lead-coordinated
  deploy).** The engine-only origin-rebase shipped now removes the x-growth pathology and makes that final
  flip a contained change.
- *Variance family (volume_zscore, pv_correlation).* Still needs the centered-sum batch change (store
  `Σ(x−c)`/`Σ(x−c)²` for a fixed per-symbol `c`) — `c` must be reproducible by the stateless batch kernel, so
  this touches `rust_windowed_sums` (invasive, Lead-owned). NOTE: on the flagged smooth-churn walk volume /
  price_volume now measure 0.00x (their breach is volume-MAGNITUDE driven and that fixture has no degenerate
  volume window); confirm on a degenerate-volume walk before relying on the variance family being flippable
  via the n==2 guard alone.
- Per-group parity test in `tests/test_fp_incremental_capture.py`: pin batch-vs-incremental < breach ratio
  on the smooth near-perfect-fit walk for each group BEFORE flipping its `incremental_safe`.
- *`test_ols_near_perfect_fit_is_flagged` is currently `@pytest.mark.xfail(strict=False)`.* PR #132's
  engine-only origin-rebase already dropped this fixture's worst batch-vs-incremental ratio on
  `trend_quality`/`clean_momentum` to ~0.41x (well under the 10x breach), so the test's "the breach is real,
  the gate is load-bearing" premise no longer holds while the flag flip is held — leaving it un-xfailed would
  pollute every agent's suite with a known-pending failure and mask real regressions. **WHEN the Lever-1 n==2
  guard + `incremental_safe` flip lands, REMOVE the xfail and rework the assertion** (those two groups become
  `incremental_safe=True`, so the current `not g.incremental_safe` filter yields an empty `sensitive` list —
  re-target the test at whatever groups remain gated, or convert it to assert the post-flip ≤0.03x parity).

**Rough effort:** ~1 focused cycle. **Expected after:** the 4 groups move off batch onto the fast path,
removing ~54ms p50 / ~91ms p99 of batch reduction work from the per-minute path (measured, 375-sym shard).
**Risk:** MEDIUM — contained to the engine, but it is the shared incremental path every ReductionGroup
rides; ship per-group behind the existing gate so any regression is one flag away from revert.

### Lever #2 (BIG, the REAL <100ms lever) — heavy-group migration (`momentum_run` + `residual_analysis`)

These two hand-written `FeatureGroup`s are ~497ms + ~247ms ≈ **43% of the live per-minute compute** and the
incremental gate does NOT touch them (they are not `ReductionGroup`s). This is the dominant remaining cost
and the lever that actually moves the full-flow number toward <100ms.

**Why they are slow:** both run a per-minute polars `rolling`/`rolling_sum_by` with `.over("symbol")` — a
per-minute symbol-partitioned sort + windowed reduction over the trailing slice (`LOOKBACK_MINUTES` = 75m).
That `over("symbol")` re-partition is the same cost class the incremental V2 work already eliminated for the
reduction tier (the ~53ms slice-derive that the Rust kernel cut to ~2.5ms — §6) and for the stateful tier
(the per-minute whole-buffer sort that `stateful.coded_buffer` cut by sharing ONE sort across groups — §6).
So the migration is NOT new research; it reapplies two proven, parity-gated patterns.

**Migration plan, per group:**

- **`residual_analysis`** (6 features, `residual_std_{w}` for w∈{5,10,15,20,30,60}) is a rolling
  power-sum OLS residual-std — STRUCTURALLY identical to `trend_quality` (already a `ReductionGroup` on the
  fast path). Path: convert it to a `ReductionGroup` declaring the residual power sums via
  `reduced()`/`regressions()` (it already computes `__one/__x/__xx/__xy/__yy` power sums — the exact shape
  `build_plan` sums) and the std-from-SSR in `assemble()`. Then it rides the SAME incremental engine for
  free (and the SAME batched marshal as the other reductions — no separate per-minute sort). The one
  subtlety is the near-linear degenerate guard (`REL_RESID_FLOOR`) which must move into the shared
  `assemble()` so live==backfill (the `trend_quality` r2-flat guard is the precedent). **This is the
  highest-ROI single move:** ~247ms → folds into the existing reduction-emit (~tens of ms shared), AND it
  likely lands `residual_analysis` as an `incremental_safe=False` group at first (its SSR is the same
  near-perfect-fit cancellation as price_r2) — so it pairs naturally with Lever #1's OLS-centering work.

- **`momentum_run`** (12 features) splits cleanly:
  - *`residual_skew_{w}` (6)* — a window-LOCAL OLS third moment (`m3/m2^1.5`). It needs two more power sums
    than the standard OLS kernel carries (`Σx²y`, `Σx³` for the centered third moment), so it is either (a)
    a `ReductionGroup` after extending the reduction kernel to emit those two extra co-moments (a modest,
    parity-gated kernel addition — the declarative path is designed for exactly this), or (b) a Rust
    `windowed_skew` kernel mirroring `rolling_extrema`/`time_lag_gather`. Prefer (a) — it reuses the shared
    marshal and the existing parity harness.
  - *`longest_streak_{w}` (6)* — a sequential run-length state machine over return signs. This is NOT a
    windowed sum; it is a per-symbol RUNNING accumulator exactly like OBV (cumulative) — i.e. a natural
    `StatefulEngine` resident (a per-(symbol) running run-length advanced one bar at a time, windowed-capped
    in emit). Path: add a `run_length` stateful spec alongside the existing `rolling_extrema` / lag kinds in
    `stateful.py`; it folds O(1)/bar like the others and shares the one `coded_buffer` sort.

**Rough effort:** MEDIUM-LARGE — ~2–3 focused cycles. `residual_analysis`→ReductionGroup is the cheap first
half (~0.5 cycle, big win); the `momentum_run` skew kernel-extension + the streak StatefulEngine resident
are the larger half. Each step is independently parity-gated (the generic `tests/test_fp_latest.py` +
per-group cell-for-cell tests) and independently shippable.

**Expected after (projection, NOT yet measured):** moving ~744ms of `over("symbol")` rolling work onto the
shared marshal + Rust kernels should track the same ~20× the reduction/stateful migrations achieved (§6:
53ms→2.5ms, 250ms→125ms). Realistic target: the two groups' combined contribution drops from ~744ms to the
tens-of-ms range, bringing the FULL 519-feature flow from its current live cost toward the sim-proven
~300–330ms p99, and the fast-path tier to the <100ms bar — i.e. THIS is the lever that closes the
production <100ms gap, not the incremental gate alone. The certified number is owed once built and measured
at the 10k steady-state (§2).

**Sequencing recommendation:** when latency is re-prioritized, do `residual_analysis`→ReductionGroup FIRST
(cheapest, biggest single win, and it co-locates with Lever #1's OLS-centering), then `momentum_run`'s two
halves, each behind its own parity gate. Do NOT batch them into one big-bang change (§4.5).
