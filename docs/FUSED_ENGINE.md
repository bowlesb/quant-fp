# Fused Live Engine — staged design

## Why
At 10k×519 the per-minute critical path is the per-shard `compute_latest`. After caching the multi-day
groups, profiling (1000 sym, 60m, 3 threads) shows **no group dominates** — the top groups
(`price_volume` 22ms, `efficiency` 20ms, `return_dynamics` 20ms) are already on Rust kernels and are now
**marshal-bound, not compute-bound**: each of the ~12 helper-using groups independently
- builds the unique symbol list and a symbol→code map,
- sorts the buffer by (symbol, minute),
- copies its value columns to numpy (`.to_numpy()`),
- calls a windowed kernel, then rebuilds a long frame and pivots.

The symbol-coding, the epoch-minute column, and the sort are **identical across all 12 groups**, and the
kernel calls could be **one** pass over all columns. That repeated marshaling is the cost the fused engine
removes — and it also cuts memory-bandwidth traffic, which is the main source of the 10-way-shard
concurrency overhead (profile ~300ms single-process vs ~900ms under 10 concurrent shards).

## Invariants this must preserve (non-negotiable)
- **`compute()` is untouched.** It stays the backfill source of truth; the whole parity guarantee rests on
  it. The fused engine only optimizes the *live* `compute_latest` path.
- **Parity validates it for free.** `tests/test_fp_latest.py` already requires
  `compute_latest == compute().filter(T)` cell-for-cell. The fused path is a third implementation the same
  test checks — so it can never silently diverge.
- **Reversible.** The fused path lives beside the existing per-group path behind a flag; both coexist.
- **Readable / decomposable.** Groups stay declarative — they DECLARE their reductions (columns + windows),
  they don't hand-roll a monolithic kernel. This is the opposite of "fast but unreadable."

## Stages (each independently shippable + parity-gated)

### Stage 1 — shared marshaling context (additive, low-risk)  ← start here
Compute the symbol→code map, the epoch-minute vector, and the sort order **once per minute** in
`process_bars`, hand it to the helpers via the context. The helpers (`rust_windowed_sums`,
`rust_reductions`, …) gain an optional `coded` argument; when present they skip the per-call
unique/sort/code-build. Zero change to group logic (they already pass `ctx`/frame). Removes 12× redundant
coding+sort. Expected: modest (tens of ms) but free and safe.

### Stage 2 — batched kernel pass (the real win)
Two-phase live execution:
1. **Declare:** each helper-using group, instead of calling the kernel, returns its *derived value columns*
   (polars expressions) + the windows it needs. (A group's `compute_latest` splits into "derive columns"
   and "assemble features from sums".)
2. **Execute:** the engine concatenates all groups' derived columns into ONE frame, symbol-codes + sorts
   ONCE, calls `windowed_sums` ONCE over all columns × the union of windows, ONE numpy marshal.
3. **Assemble:** each group reads its columns' sums back and computes its named features (the existing
   algebra).

This is where the per-group marshal/sort/kernel overhead collapses to a single shared pass. Group code
becomes MORE declarative (a list of `(name, expr)` + windows), not less readable. Rollout is group-by-group
behind the flag, each validated by the parity test as it moves over.

### Stage 3 — single-pass sequential kernels (optional, later)
The few sequential groups (`technical` EWM/MACD, `tick_runlength`, `microstructure_burst`) get small Rust
recurrence kernels so they stop recomputing the whole buffer in polars on the live path.

## Risk / reversibility summary
- Touches only `compute_latest` + the `latest.py` helper layer; `compute()` and the store/parity harness
  are untouched.
- Every stage is flag-gated and parity-validated, so it can be turned off or rolled back per group.
- The readability cost is controlled by keeping groups declarative (declare reductions, don't hand-code).

## The constraint that reshapes this: it must serve MODELING/BACKFILL too, not just live

The win can't be live-only. The modeller loop is: *write a weird new feature fast → fit it in the harness
→ have all data on disk → backfill an enormous dataset in minutes → iterate.* The engine has to make that
loop fast AND speed production live. Three consequences for the design:

1. **One feature definition feeds both forms (kill the double-write).** Today a group writes `compute()`
   (rolling, backfill) AND `compute_latest()` (at-T, live) and the parity test checks they agree — double
   work per feature, and a parity trap. For the common case (windowed reductions/lags/ranks), a feature
   should DECLARE its reduction once; the engine GENERATES both the rolling (backfill) and the at-T (live)
   evaluation from that one declaration — so parity is by construction (nothing to diverge) and the
   modeller writes it once. This is the single biggest iteration-speed win.

2. **Two tiers, because modellers prototype bizarre things.**
   - **Declarative tier** (most features): declare `(derived columns, windows, assembly)`. Engine generates
     live + backfill, batches them (one marshal/sort/kernel pass over all such features), parity-free.
   - **Arbitrary tier** (escape hatch): a genuinely weird feature writes plain polars `compute()`. Slower,
     not batched, but ZERO friction to write — the modeller is never blocked by the framework.
   A research feature may start **backfill-only** (just `compute()`), get its enormous training set, and
   only add the live form (+ parity) when it's promoted to production. The harness must allow that.

3. **Backfill runs on the SAME sharded/parallel engine as live.** The reason a new feature backfills "in
   minutes" is that it fans out across all cores the same way live fans across shards. The fused engine is
   the shared execution core both call — so an optimization (batched kernels, one marshal) is felt
   identically in a modeller's overnight backfill and in the Monday live loop. Build it once, both benefit.

**GPU — SPIKED AND RULED OUT for feature compute (2026-06-14).** Tested the polars GPU engine
(`cudf-polars`, RTX 3090) on representative backfill ops at 1.17M and 3.9M rows. Result: GPU is SLOWER, and
our core op is unsupported.
- cross-sectional rank: GPU 6×/2.4× SLOWER than CPU (transfer+launch overhead dominates at these sizes;
  multithreaded CPU polars wins on 1–4M-row batches);
- cross-sectional agg: GPU ~1.7–2.5× slower;
- **`rolling_*_by`** (the time-windowed rolling EVERY backfill feature is built on): **not implemented in
  cudf-polars** → silent CPU fallback.
- Parity was fine where GPU ran (max |CPU−GPU| 1e-16…1e-13, within tolerance) — so parity is NOT the
  blocker; performance/support is.
Conclusion: the CPU path (Rust single-pass kernels + sharding + this declarative engine) is both faster and
readable; a GPU win would require rewriting features in cudf (the readability cost we're avoiding). The
3090 is better spent on model TRAINING (large dense matmuls — actually GPU-ideal), not feature compute.
`docker/fp-gpu.Dockerfile` is kept for that future training use.

## Expected payoff
- **Live:** single-process per-shard compute ~300ms → ~100ms (one marshal instead of ~12), less memory
  traffic shrinks the concurrency gap — full 10k vector toward ~100ms p99.
- **Modeling:** write-once features (no double-write, parity free), backfill-only research mode, and a
  sharded+(optionally GPU) backfill that turns "an enormous dataset" into minutes — the same engine, felt
  in both production and the ad-hoc loop.
