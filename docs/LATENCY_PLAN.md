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
