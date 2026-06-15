# 2026-06-15 ~14:30 ET — CRITICAL-3 fixed: breadth runs in the whole-universe gather (was per-shard)

## FOR THE OWNER (top)
- **No live action needed; nothing deployed.** This is a FIXED-CODE feature-value change, merged to
  `integration/converged` but **NOT** applied to the running capture (the process only reloads code on
  `docker restart feature-computer`). Per the SEQUENCING RULE it ships on the ONE batched clean restart
  alongside CRITICAL-1 (points) + CRITICAL-2 (warm-start) — do not restart for this alone.
- **After that restart:** delete & recollect all `source=stream` breadth (the per-shard data is corrupt).
- **Still systemic / not fixed here:** breadth's `sector_breadth_*` tier stays dead until HIGH-SECTOR
  (empty `sector_map`, FMP ingestion) lands, and breadth still inherits the P0-UNIVERSE ETF contamination
  in the stream universe. Both are separate backlog items.

## Maintenance (this cycle)
Healthcheck: **11 PASS / 3 WARN / 1 FAIL**. System healthy — newest minute 2.2 min old, 8/8 shards UP,
universe 11,336, coverage 89.0%, alphabetical bias 31.9% (normal). The 1 FAIL (`bar_to_vector_latency`
60s) is the known batch-path floor (fixed by P1 #1, the fast path). The 3 WARNs are known/tracked:
`group_compute_p99` slow, validation ledger empty (P1 #2), trust grades empty (P1 #2). No safe-fix needed.

## What I advanced (CRITICAL-3, my lane = engine/capture)
`breadth` is a market-wide GATHER reduce (fraction of the universe up/down, + per sector), but
`REDUCE_GROUPS` listed only `cross_sectional_rank` — so breadth ran inside each of the 8 shard workers
over only ~1/8 of the universe. Every "market-wide" scalar took 8 distinct values per minute (symbols
partitioned by `hash%8`); backfill is single-process ⇒ live≠backfill, all 30 `breadth_*`/`sector_breadth_*`
columns corrupt.

**Fix (`7f9a357`, `fix/breadth-reduce-whole-universe` → integration/converged):**
1. Added `"breadth"` to `REDUCE_GROUPS` — workers exclude it; the reader's gather computes it once over
   every symbol.
2. Threaded the reader's **full un-sharded `snapshots`** (reference+daily) through
   `process_reduce`→`process_bars`. breadth needs `reference` (sector) and `daily` (close) frames to
   self-select (`runnable`) and to compute its sector + 1d/5d horizons over the whole market. (Previously
   `process_reduce` passed no snapshots, since `cross_sectional_rank` needs none.)
3. Added `breadth.reduce_buffer_minutes()` = `max(MINUTE_WINDOWS)` = 60 so the reader's minimal reduce
   ring reaches the deepest intraday horizon. The 1d/5d horizons read the settled daily snapshot, not the
   minute ring, so they impose no extra minute-buffer depth.

**Parity (sacred):**
- `test_breadth_via_reduce_identical` — reduce-routed breadth == single-process breadth cell-for-cell
  (the live↔backfill standard the per-shard form broke).
- `test_breadth_market_scalar_is_single_valued_per_minute` — exactly one market-breadth value per minute
  (the direct inverse of the 8-distinct-values symptom).
- Updated `test_cross_sectional_rank_via_reduce_identical` (REDUCE_GROUPS assertion) and
  `test_reduce_group_absent_from_shards` (now asserts ALL reduce groups absent from shards).

**Verification:** `test_fp_sharding` + `test_fp_breadth` + `test_fp_latest` green; warm_start, stream_sim,
incremental, sharding green; **full suite 300 pass / 27 skip** (the 2 new tests added to the prior 298).
ruff not installed in fp-dev/host this env → manual style review (changes mirror existing idioms).

## Next step (for the next cycle)
With CRITICAL-3 done, the remaining MY-LANE P1.0 systemic items are CRITICAL-2 close-out (flip
`FP_WARM_START=1` at the clean restart — a deploy decision, owner-gated) and MED-DEDUP (single-writer /
dedup-on-write for broadcast symbols). After P1.0, the greenlit P1 queue resumes at #1 (incremental
fast path: wire `IncrementalEngine` into `process_bars` behind `FP_INCREMENTAL`, default OFF).
