# Step 1 plan — ONE feature (price_volume) polars-free, demonstrated fast + correct

**For Ben's approval before any code.** Scope per Ben: take ONE feature — `price_volume`, the hardest (the
parked 71–114ms group, and the proven 2.0ms keystone) — to a polars-free numpy hot path **in production**, and
**demonstrate** the real live speed. This is a single-feature demonstration, flag-gated and reversible — **not**
the migration. The broad order (gathers / swing / hand-written) is explicitly out of scope; we do one feature,
prove it on real production code, and then decide.

---

## 1. The minimal spine — only what price_volume needs

Not the full container for all fold-kinds. Only the pieces this one feature requires:

- **A fixed per-shard symbol index** (symbol → row), built once per session.
- **The carried windowed-sum state** — this already exists (`WindowedSumState`): running sums per
  (window, symbol, col), Neumaier-compensated, O(1) fold per minute (`new − evicted`). We reuse it, not rebuild it.
- **A churn count-mask** — a symbol absent this minute contributes zero, no re-index (the absent-as-zero rule).
- **Readiness** — the existing `populated` per window: an under-warmed cell is withheld, not emitted as a partial.

No EMA / extrema / state-machine kinds, no gather L2 relocate — those belong to *later* features and are not built
here. The spine is exactly "symbol-index + the windowed-sum payload + churn + readiness," scoped to price_volume.

Grounded in Ben's `tracker.py` / `vector_store.pyx` discipline: carried aggregates maintained on add, reads O(1)
off them, polars off the hot path.

## 2. The numpy step — what wires in, for price_volume only

Today price_volume's minute runs through `compute_reduction_batch` → `batch.sort` + `batch.derive` +
`rust_windowed_sums` + `assemble(pivot+join)` + `resolve_points` — **all polars** (the measured 8.97ms tax =
`matrix_at 3.78 + resolve_points 3.15 + assemble 2.04`). The polars-free step replaces that, for price_volume
only, with:

1. **numpy DERIVE** of price_volume's value matrix — the 5 reduced cols (`vol/cv/mfv/up/dn`) + the pv-corr OLS aux
   (with the y-centered volume per `regression_y_anchor`) + the obv-slope aux (time-x + the **real carried OBV
   cumulative** — the one production delta vs the keystone's placeholder). Plain numpy array ops on the incoming
   bar, **zero polars**. The exact derive (the spike's proven shape, with the production fix in place):
   - `ret = close/prev_close − 1`; `rng = high − low`; `mfm = where(rng>0, (2·close−high−low)/rng, 0)`
   - reduced: `vol`, presence `1.0`, `cv = close·vol`, `mfv = mfm·vol`, `up = where(ret>0, vol, 0)`,
     `dn = where(ret<0, vol, 0)`
   - pv-corr aux: `y = vol − anchor_volume` (the y-centering = the corr-denom conditioning), then
     `b=1, x=ret, xy=ret·y, xx=ret², yy=y²`
   - obv-slope aux: `obv = carried_obv + where(ret>0, vol, where(ret<0, −vol, 0))` — **the real carried cumulative,
     session-reset, maintained per-symbol on the ring (the one thing the keystone faked as `obv_state=0`)**;
     `centered_t = (minute_epoch − t_origin)/60` (conditioned by `rebase_time_axis`); then `b=1, x=centered_t, xy, xx, yy`.
2. **carried-ring FOLD** — the existing `WindowedSumState.update` (new − evicted, O(1)).
3. **numpy ASSEMBLE** — the existing `emit_numpy` (already proven byte-identical, #454 / #448) producing the result.

**Where it hooks (refinement from the spike):** the numpy path lives in the **engine step**, not in
`ReductionGroup.compute_latest`. `FP_STATE_SPINE` on → the numpy derive replaces `_matrix_at`'s polars
`_derived_row` and feeds the value matrix to the **existing** `emit_numpy` read surface (the #44-gated assemble —
reused, not a new one); off → today's polars `_derived_row`. One seam in the engine, riding the already-proven
numpy read surface. (This also kills Ben's per-minute re-sort — `sort` runs twice/min on the polars path; the
carried ring has fixed positions and never sorts. The before→after asserts `sort` call-count 0 on the AFTER path.)

Per-minute **compute = zero polars** (the proven 0.16ms). The only residual polars is the bus output-frame
(~0.02ms) — a bus-contract boundary, not the per-minute tax, separable and eliminable later.

## 3. The flag

`FP_STATE_SPINE`, **default OFF**. Off = today's exact path (byte-identical by construction — the new path isn't
entered). On = the polars-free numpy step for price_volume. Same idiom and warm-start deploy seam as
`FP_POINT_RING` / `FP_RUST_REDUCE` — **mergeable without changing live behavior**, armed + relaunched separately
under the live parity gate. `fp` / fingerprint UNCHANGED (value-identical refactor → no coordinated strategy
redeploy).

## 4. How #451 gates it — byte-identical-or-revert

Every push runs the full gate, in order; any red → does not ship, fix or revert:

1. **#451 demolition value gate, price_volume rows** — the new step's output vs backfill `compute().filter(last)`
   (the source of truth that can't drift), at **FR=0 and FR=1**, on the sparse/gappy fixture:
   - **isolated** (price_volume alone) — per-group byte-identity.
   - **co-resident** (price_volume + a time-OLS group sharing the engine) — **the load-bearing check**: it
     exercises the corr-denom straddle on the flat `Σxx≈0` degenerate cell, exactly the conditioning that parked
     price_volume. The production delta (real OBV cumulative + y-anchor/time-OLS conditioning vs the keystone's
     placeholder `obv_state=0`) would surface **here** if it diverges. This cell is watched specifically.
2. **`test_fp_incremental_emit`** — the dedicated "numpy read surface (`emit_numpy`) == batch `compute_latest`"
   proof for price_volume (every accessor: sum / std / corr-denom / r² / mean_y / OLS), FR0+FR1.
3. **The full suite stays green** — the existing 184 #451 tests + the other 63 groups' gate unchanged (the spine
   scaffolding must regress nothing).

## 5. How it's armed + measured live — the demonstration

After the Lead's grading + merge (flag still default-off, so merge is safe): **DeployRefactor** arms
`FP_STATE_SPINE` on **one canary shard** under the live parity gate, relaunches via the warm-start seam, and we
capture the **real before→after on live fc bars** (not the throwaway), cpuset-pinned + bounded:

1. **Per-feature step ms (the headline)** — price_volume's per-minute step, before (current path) vs after
   (polars-free), warm steady-state p50/p99.
2. **cProfile compute-collect = 0** — the per-minute COMPUTE has zero polars collect (the tax deleted, not
   trimmed); any residual collect is the output-frame (~0.02ms), stated honestly.
3. **Per-shard delta** — price_volume's contribution to the shard's per-minute cost before vs after: the first
   concrete proof on real data that one feature's polars-free conversion moves the shard number (a slice of the
   ~95%-eliminable tax — the `<300ms` story's first real-data evidence).

The PR reports **measured truth** on real bars, not the throwaway's 2.0ms.

## 6. Expected live number (honest, from the keystone)

price_volume's per-minute step from **~71ms** (parked batch) / **~14ms** (armed-incremental, still-with-tax) →
**~2.0ms** (polars-free, compute 0.16ms) = the **7×** the keystone proved, now on the real production wiring. The
~0.02ms bus output-frame remains (eliminable later if the bus takes numpy).

## 7. Deliverable + division of labour

**One PR** — spine (minimal) + price_volume's polars-free numpy step, flag default-off — for the Lead's grading,
with #451-green + the before→after live number + the compute-collect=0 split. No other group touched.

- **ArchOverhaul** builds: the minimal spine + price_volume's real value-correct numpy derive end-to-end, flag-gated.
- **CriticalProfiler** gates + measures: the full #451 gate (64 groups × FR0+FR1 × isolated+co-resident +
  `test_fp_incremental_emit`), watching the co-resident conditioning cell; and the live before→after demonstration.
- **Box discipline** (it OOM'd today): cpuset-pinned, one job at a time, `GATING`/`BUILDING` pings, wait-for-idle.

## What Ben sees

ONE feature — the hardest, with the conditioning crux — proven **fast** (~2ms, compute-collect=0) **and correct**
(byte-identical to backfill, #451 incl the conditioning cell, byte-identical-or-revert) on **production code**,
flag-gated and reversible. A demonstration that the polars-free hot path is real on the real wiring — before any
migration. The keystone already proved the target value-identically; this plan shows *how* we prove it live, on
one feature, with a reversible flag.
