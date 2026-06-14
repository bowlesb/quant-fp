# Incremental live engine — design, critique, test plan

## Goal
Make the per-minute LIVE compute do only the *exact, minimal* work at the minute mark, by holding running
state between minutes — without changing any feature's core logic and without weakening parity. Target: the
per-shard 617ms → low-tens-of-ms.

## The one principle everything hangs on
A feature group declares its **core logic once**: `reduced()` / `regressions()` / `points()` / `assemble()`.
That NEVER changes. What changes between code paths is only **how the windowed sums are obtained**. All paths
produce the *same canonical columns* (`__sum_<c>_<w>`, `__mean_<c>_<w>`, `__slope_<r>_<w>`, …) and then run
the *same* `assemble()`. So the three execution paths are interchangeable sum-sources behind one contract:

| path | how sums are obtained | when | source-of-truth? |
|---|---|---|---|
| **Backfill** (`compute`) | polars `rolling_*_by` over full history | T+1 batch / modeller jobs | **YES** (defines truth) |
| **Live batch** (`compute_reduction_batch`) | Rust kernel re-scans the trailing buffer at T | today's live path | no (parity-checked vs backfill) |
| **Live incremental** (NEW) | `WindowedSumState`: fold new minute, expire old | new live path | no (parity-checked vs both) |

Parity chain (already half-proven): backfill == live-batch (`test_fp_latest`) == live-incremental
(`test_fp_incremental` at the sum level; this design adds the feature-level check). Because every path is
windowed sums and the sum is the only thing that differs, agreement is by construction, not a bolt-on.

## Data flow — the live incremental path (the new one)
Per shard, the worker holds ONE `WindowedSumState` covering **all declarative groups' value columns** (the
union — same columns the batch concatenates today), plus the trailing bar buffer (already kept).

Each minute T:
1. **Derive the NEW minute only.** Compute every group's value-column exprs over just the last `K` minutes
   of the buffer (`K` = max intra-value lag + 1 ≈ 4; e.g. `ret = close/close.shift(1)-1`, OLS products,
   power-sums — all short-lag) and take row T → an `(n_symbols, n_cols)` matrix (nulls→0). *Not* 75k rows.
2. **Fold it in.** `state.update(epoch_T, matrix)` — add to every window's running sum, expire minutes that
   left each window. Measured **0.49ms**.
3. **Build canonical columns from the running sums.** The running sums are already indexed
   `[window, symbol, col]` — i.e. already "wide", so the canonical frame is a direct construction (NO pivot,
   which was 27% of the batch). `mean = sum/presence_sum`, `std` from `sum`/`sumsq`/`presence`, OLS stats
   from the six paired sums — the SAME algebra as `_canonical`/`_ols_stat_exprs`, just reading running sums.
4. **Assemble.** The group's `assemble()` exprs over the canonical frame + `points()` (latest-minute scalars).
   Unchanged.

So the minute mark eliminates BOTH big costs: the buffer re-scan (derive, step 1 is one minute) AND the
pivot (step 3 is a direct numpy→wide construction).

## State lifecycle
- **Seed.** On worker start (buffer may already hold minutes — e.g. a restart mid-session, or a backfilled
  warmup buffer), seed by folding every buffered minute into the state (== the batch recompute over the
  buffer; the accumulator test proves equality). Empty buffer → folds naturally as minutes arrive (warmup).
- **Drift / resync.** Incremental float sums drift slowly; per trading day the accumulation stays far inside
  tolerance (sums of ~1e-3..1e6 magnitudes over ~390 add/subtract steps ≪ each feature's 1e-6..1e-2 tol). To
  be safe and to recover from any divergence, **re-seed daily** (reset + replay the buffer = one batch
  recompute). This is also crash recovery: lose the state, rebuild it from the (durable) buffer.
- **Symbol churn.** Within a session the shard's symbol set is ~fixed (new listings are a daily event); the
  value matrix each minute is symbol-aligned to the state. New/removed symbols are reconciled at the daily
  re-seed. (V1: fixed symbol set per session, asserted; V2: in-session add/remove via index growth.)

## Backfill path — explicitly NOT incremental, and why that's fine
Backfill (`compute`) stays the vectorized polars rolling form over a whole history range. It is the SOURCE
OF TRUTH and runs offline (latency-insensitive), so it has no reason to hold minute-by-minute state — it
sees all minutes at once. The two paths therefore differ in execution but compute the SAME feature from the
SAME declaration, and the parity test pins them together. (A modeller's backfill of a NEW feature also uses
this path — unchanged, so iteration speed is unaffected.)

## Critical review — where this can go wrong, and the mitigation
1. **Float drift breaking parity** over a long session. → Bounded per day; **daily re-seed** resets it. Test
   must run a full session length and assert tolerance holds at the end.
2. **The "derive new minute" must be byte-identical to the batch's derive.** Same exprs, but computed over a
   short slice instead of the buffer — a `shift(k)` near the slice's start could read a different/absent
   prior bar. → Derive over `last (max_lag + buffer_safety)` minutes, not exactly `max_lag`; assert the
   derived row equals the batch's row for that minute (a test).
3. **Window-boundary / gap semantics** must match the kernel exactly (minute at `T−w` excluded). → Already
   proven equal in `test_fp_incremental`; the integration test re-checks at the feature level.
4. **Presence/count for mean/std and OLS pairing under nulls.** The state must fold the same presence/square/
   product columns the batch does, so `mean=sum/presence` etc. stay null-correct. → The value-column set fed
   to the state IS the batch's `value_cols` (incl. `__p`, `__sq`, OLS `b/x/y/xy/xx/yy`). One source of truth
   for "what columns to sum."
5. **Symbol set changes mid-session** would misalign the matrix. → V1 asserts a fixed set per session and
   reconciles at re-seed; loudly errors if violated rather than silently corrupting.
6. **Non-declarative groups** (cross_sectional_rank reduce, calendar, sector, multi_day, technical EWM, …)
   are NOT windowed sums → stay on their current path. The incremental engine only replaces the declarative
   batch. The minute's total = incremental declarative + the (already cheap/cached) rest.
7. **Two code paths drifting in maintenance.** → They share `reduced/regressions/points/assemble` and the
   canonical algebra; only the sum-source is separate. A new feature can't diverge because it only writes the
   shared core. Enforced by the test that ALL three paths agree on every registered declarative group.

## Test plan (demoes both code paths)
- **(have) `test_fp_incremental`** — accumulator sums == kernel sums, cell-for-cell.
- **NEW `test_fp_incremental_features`** — for each declarative group: feed a synthetic minute stream through
  the incremental path and assert the per-minute feature frame equals `compute_latest` (batch) within the
  group's tolerance, at several minutes (warmup + full-window). This is "the two live paths agree."
- **NEW derive-equivalence test** — the "new minute only" derived matrix equals the batch's derived row for
  that minute (guards mitigation #2).
- **NEW session-length drift test** — run a long minute stream; assert incremental == batch at the END still
  within tolerance (guards #1), and that a re-seed restores bit-closeness.
- **(have) `test_fp_latest`** — batch == backfill, unchanged. Together: backfill == batch == incremental.

## STATUS (branch feature/incremental-engine)
- **V1 BUILT + PARITY-VALIDATED.** `IncrementalEngine` (`incremental.py`) + shared `build_plan` /
  `assemble_from_long` (the batch and incremental now run the SAME assemble code; only the sum-source
  differs). `tests/test_fp_incremental_features` proves `step()` == `compute_latest()` cell-for-cell across a
  minute stream for every declarative group. The test caught a real bug: **cumulative columns (OBV =
  cum_sum) can't be slice-derived** — V1 derives the new minute over the whole buffer (correct).
- **V1 speed: 91.6 → 83.5ms** after the one-pivot assemble (vs batch 121.6ms, 1.46×) at 1250×60. The fold is
  0.49ms; what remains is the **whole-buffer derive (62ms)** — the last big cost.
- **One-pivot assemble DONE** (helps both paths): `assemble_from_long` now does a single multi-value pivot
  per group instead of one pivot+join per stat. (Watch: a single-value pivot drops the value name → a dummy
  const keeps ≥2 values.)
- **V2 (slice-derive) — BUILT + PARITY-VALIDATED.** `step()` now slice-derives: the short-lag value columns
  (ret, products, power-sums, presence/square) are derived over a ~6-minute slice in ONE lazy polars pass,
  and the long-history regressor columns are maintained as running per-symbol engine state, declared via
  `ReductionGroup.stateful_regressors()` (default empty — backfill/live-batch ignore it). Two kinds:
  - `kind="time"`: a frame-relative OLS time axis. Slice-derive can't reproduce a frame-relative origin, so
    the engine substitutes a FIXED seed origin (OLS is origin-invariant → same slope/r2/corr within tol).
    Used by `trend_quality.trend` and `price_volume.obv` (x slot).
  - `kind="cumulative"`: a running total `v[T]=v[T-1]+increment[T]` (OBV). The group declares the short-lag
    `increment` (`signed`); the engine keeps the running per-symbol total. Used by `price_volume.obv` (y slot).
  The engine rebuilds the 6 OLS paired columns (b,x,y,xy,xx,yy) for these regressions from the running x/y
  with the SAME pairing-under-nulls as `_ols_derived`, so the value matrix is identical to the whole-buffer
  derive. Guarded by `test_slice_derive_matches_whole_buffer` (matrix == V1 whole-buffer derive, cell-for-cell)
  + the existing feature-parity test (`step()` == `compute_latest()`).
- **V2 measured (1250×60, 2 CPUs, reproducible):** fold (slice-derive + state.update + running_long) dropped
  from V1's ~67ms to **~54ms CPU-bound / ~35ms unconstrained**; full step (fold + shared assemble) 97ms (V1)
  → **80ms (V2)**. The fold's remaining cost is the 43 `over("symbol")` slice-derive expressions (≈0.5ms each,
  fixed per-expr partition overhead on 1250 groups, not row count — the slice is only ~7.5k rows). The shared
  `assemble_from_long` (pivot + feature exprs, ~26ms CPU-bound) is now co-dominant; it is the SAME code the
  batch runs, so it is not an incremental-specific cost. A combined single-pivot-across-all-groups was
  prototyped and was SLOWER (one big pivot > 11 small ones), so the per-group pivot stays.
- **Not yet <50ms full-step.** Two remaining levers, both out of the V2 fold's scope: (a) cut the
  `over("symbol")` per-expression overhead in slice-derive (share the common `ret` once, or a Rust slice
  kernel), (b) collapse the assemble onto the numpy running sums directly (would reimplement each group's
  `assemble()` outside polars — large + parity-risky; `assemble_from_long` is deliberately left unchanged).
- **REMAINING after V2:** wire into the worker behind `FP_INCREMENTAL` (seed on start/daily, step per
  minute), the session-length drift test, and symbol-churn V2.

## Implementation steps (each independently committed + parity-gated, revertible on this branch)
1. `IncrementalEngine`: holds the per-shard `WindowedSumState` + the union value-column plan built from the
   declarative groups; methods `seed(buffer)` and `step(new_minute_bars) -> {group: feature_frame}`.
2. The "derive new minute" + "canonical from running sums" reusing the existing `_canonical`/`_ols_stat_exprs`.
3. Wire into the worker behind a flag (`FP_INCREMENTAL`), default OFF → zero risk to the working batch path;
   flip on for the bench to measure.
4. Tests above. Then make it the default once green and benched.
