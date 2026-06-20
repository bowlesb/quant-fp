# Reconciliation — Ben's prior feature abstraction vs UNIFIED_STATE_EXECUTION_SPEC

> Status: RECONCILIATION for gate-read (2026-06-20). Author: Latency. Studies Ben's prior codebase
> (`/home/ben/automated-day-trading/scode/features`, branch `feb22`, READ-ONLY) as a CONCEPT — NOT a
> proven design to copy. Extracts the conceptual spirit (kind decomposition, static-vs-incremental
> split, declare/need/emit contract) and states where we implement it BETTER. Reconciles against
> docs/UNIFIED_STATE_EXECUTION_SPEC.md (#278) + STATE_ABSTRACTION.md.

## TL;DR

Ben's prior code VALIDATES our spec's core concept — features decompose into a small set of KINDS, each
with a once-vs-per-bar split and a declare/need/emit contract — but his IMPLEMENTATION is a pyspark/pandas
BATCH re-compute with no true incremental live state, positional vector assembly, and a coarse
"re-partition the groupby" stand-in for the static/incremental split. **We keep the concept and implement
it strictly better: an explicit `seed/fold/emit` state engine with a parity invariant, a named-offset
fingerprinted vector, an engine-owned per-session cache, and Rust-resident folds where hot.** The #278
classification stands; one genuinely-new KIND to adopt (the CHUNK/path-segmentation kind) and one contract
refinement (his `need_columns`/`return_schema` declaration is cleaner than scattering inputs — we already
have it via `InputSpec`/`declare`, keep it).

## What Ben's code actually is (the concept, file by file)

- **`job/base.py :: Job.add_feature_wrapper(func, return_schema, need_columns, agg_cols)`** — the HEART.
  A feature DECLARES its inputs (`need_columns`), outputs (`return_schema`), and partition (`agg_cols` =
  `("Date","ticker")` for intraday, or `("ticker", month_col)` for slow features). The engine selects only
  needed columns, partitions, applies `func(pdf)` on a per-group time-SORTED pandas frame, merges back.
  → This IS the declare/need/emit contract. The `agg_cols` granularity swap (per-day vs per-month) is his
    stand-in for the STATIC-vs-INCREMENTAL split: slow features partition coarser so they recompute less.
- **`emas/emas.pyx :: get_emas`** — the EMA KIND: a forward recursion `v=(1-α)·old+α·price` over a running
  per-halflife dict, MACD built on top. A single batch forward pass; no separate live fold.
- **`chunk_characterize/chunks2.pyx :: compute_chunks` + `get_features`** — the CHUNK KIND: walk BACKWARD
  from the latest bar, segment the path into directional monotone "chunks" (bounded `n_chunks_allowed`),
  then characterize each chunk with `quadratic_fit_with_p_value` (curvature + fit p-value) over a
  `custom_gaussian_smooth`ed series. ~95 path-geometry feats. This is the ancestor of our swing / swing_dc.
- **`window_abs/run.py`** — the WINDOW KIND: `window_diffs(close, windows)` over `[2..300]` windows, the
  same windowed-difference family as our additive-window reductions.
- **`feature_vector.py :: get_final_feature_vector`** — vector assembly = `np.concatenate([single_feats] +
  group_feats)` with positional names `f"{key}_{i}"`.
- **`news/real_time.py`** — the "real-time" path is `real_time_news()` re-running the batch build. There is
  NO incremental live-state engine; live == re-run.

## Mapping to OUR spec (concept matches)

| Ben's concept | Our spec / code | Verdict |
|---|---|---|
| `add_feature_wrapper(need_columns, return_schema, func)` | `InputSpec` + `declare()` + `compute()` | SAME contract — ours already typed + fingerprinted |
| `agg_cols` per-day vs per-month (static/slow split) | Class A (intraday-invariant, SessionCache) vs Class B (fold) | SAME split, ours is explicit + finer (once-per-session, not coarse re-partition) |
| EMA forward-recursion dict | `EMAState` (KIND-B) | SAME kind; ours adds the live fold + parity invariant |
| chunk/path-segmentation + per-chunk fit | swing / swing_dc (bespoke today) | CONCEPT we should promote to a declared KIND |
| `window_diffs` over many windows | `WindowedSumState` / ReductionGroup | SAME kind; ours is parity-true incremental |
| `np.concatenate` positional vector | `BusSchema` named-offset + 64-bit fingerprint | ours STRICTLY better (named, versioned, parity-checked) |
| `real_time_news()` = re-run batch | `seed`/`fold`/`emit` live engine + `seed(H);fold(m)==seed(H+m)` | ours STRICTLY better (true incremental, parity by construction) |

## What we ADOPT from the concept

1. **The explicit declare/need/emit contract as the universal author surface** — Ben's `need_columns`/
   `return_schema`/`func` confirms our `InputSpec`/`declare`/`compute`+`emit` is the right shape. KEEP ours
   (typed, fingerprinted); no change needed — his code validates the direction.
2. **The CHUNK / path-segmentation KIND** — this is the one genuinely-new kind worth promoting. Our swing /
   swing_dc are bespoke FeatureGroups; Ben's `compute_chunks` (backward-from-latest, bounded chunk count,
   per-chunk quadratic-fit) is a clean primitive. ADD a declared "chunk/path-geometry" KIND to the engine
   (with its `seed(H);fold(m)==seed(H+m)` parity test) so swing-family features declare it instead of
   hand-rolling. Naturally latest-anchored (bounded lookback) → fits the latest-only pattern we've shipped.
3. **The static/slow partition idea**, but REALIZED as our SessionCache (the P1 build) rather than a
   coarse groupby re-partition. Confirms Class A is right.

## Where we implement BETTER (his impl is the weak part — improve it)

1. **True incremental seed/fold/emit, not batch re-run.** Ben's EMA and "real-time" both recompute the full
   forward pass every time. Our `seed(buffer)` once + `fold(minute)` O(state) + `emit()` is the correct
   live path; the `seed(H);fold(m)==seed(H+m)` invariant is what makes it parity-true — Ben has no such
   guarantee (his live just re-runs the batch, so "parity" is trivially true but SLOW, never O(1)/min).
2. **Named, fingerprinted vector** vs his positional `f"{key}_{i}"` concat. Our BusSchema + 64-bit
   fingerprint catches schema drift at the bus boundary; his positional assembly silently misaligns on any
   feature add/remove.
3. **Parity by construction across live/backfill** is our non-negotiable (STATE_ABSTRACTION §"principle").
   Ben's batch-only world never had a separate live path to keep parity with — so the hard part (live≠
   backfill drift) is a problem we solve that his design never faced. Our contamination-aware trust sweep +
   `test_fp_latest` byte-equality guard are the machinery his concept lacked.
4. **Rust-resident fold where hot.** Ben's kernels are Cython single-pass batch. Our P3 (Rust-resident
   per-minute fold over the shared coded buffer, advancing ALL same-kind groups in one call) is the lever
   that removes the per-group Python-frame floor — a step beyond his per-feature Cython UDF.
5. **Cancellation-safe incremental sums (P2).** Ben recomputes windows each batch (no drift). Our
   incremental WindowedSumState needs Welford/Kahan on the ~9 Σx²−(Σx)²/n groups to be parity-true as the
   DEFAULT — a problem created by going truly-incremental (which is the point), solved by stable summation.

## Classification changes (vs #278)

- **NO change to the A/B split** — Ben's `agg_cols` granularity confirms it.
- **ADD a "chunk / path-geometry" KIND** to the kind table (STATE_ABSTRACTION §"state KINDS") for the
  swing / swing_dc / draw_range family, modeled on `compute_chunks` (backward-from-latest, bounded chunks,
  per-chunk fit). Held as a P3/P4 kind, gated on its `fold==reseed` parity test. (swing_dc itself is
  de-staged for being a directional null — the KIND is still the right home for the path-geometry family.)
- **EDGAR stays the hybrid event-kind** (confirmed last cycle: intraday `available_at<=minute` gate → NOT
  plain Class-A). Ben's news/real_time is the analogous event-driven path; he re-runs the batch, we should
  cache-and-invalidate-on-event (or leave computing, since it's cheap — measured ~per the P1 note).

## Net

The concept is sound and our spec already embodies it — Ben's prior code is corroboration plus ONE kind to
adopt (chunk/path-geometry) and a reminder that the declare/need/emit contract is the right author surface.
Everything that makes OUR version harder (true incremental fold, live/backfill parity, named fingerprinted
vector, Rust-resident) is also what makes it BETTER than the prior batch attempt. P1 (the SessionCache
unification) is unaffected and proceeds; the chunk-KIND addition is a P3/P4 item for the kind table. No
contract or A/B reclassification is required by his design.
