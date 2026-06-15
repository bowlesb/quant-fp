# 2026-06-15 ~14:00 ET — CRITICAL-2 buffer-depth RESOLVED + 2 trunk-red tests fixed

## FOR THE OWNER (top)
- **Nothing risky touched on the live system.** No capture restart, no data dropped. Healthcheck green
  (freshness 2.4 min, 8/8 shards, coverage 88.2%, alphabetical bias 32%). The standing `bar_to_vector_latency`
  FAIL is the known batch floor (60s minute-close wait) → fixed by P1 #1, not a regression.
- **Heads-up (was already broken on trunk, now fixed):** `integration/converged` had **2 failing tests**
  in `tests/test_fp_stream_sim.py` (pre-existing, from the CRITICAL-1 resolve_points merge — the
  `__pt_*` point columns weren't wired into the incremental/stream-sim emit path). Fixed this cycle.
  This is the trades+quotes simulator path, **not the deployed live path**, so live collection was unaffected.
- **One deploy-time action still pending (SEQUENCING RULE, do NOT do piecemeal):** the buffer bump below
  ships on the next capture restart. Per the ledger, batch it with `FP_WARM_START=1` + the other FIXED-CODE
  feature fixes into ONE clean restart, then recompute today's contaminated data from backfill.

## What I advanced (one item, made it count)
**CRITICAL-2 buffer-depth question — RESOLVED** (the explicit NEXT step blocking the clean-restart sequencing).

The owner left two options ("bump to ~410m and measure" / "session-anchor the cumulative groups"). I measured:
- swing's `n_pivots_today` / `minutes_since_pivot` reset at the **UTC calendar day** (rust kernel
  `minute // 86400`), and the live fold runs over the whole buffer. Backfill folds the entire collected
  day, so for **live == backfill** the live buffer must reach the **first collected bar of the UTC day**.
- Collection starts at **premarket ~08:00 UTC (4:00 ET)** (verified from the store: earliest stream minute
  08:00 UTC). At the 16:00 ET close (20:00 UTC) that is **~720 minutes** back.
- ⇒ **410m is INSUFFICIENT** (the key finding). The old 300m buffer is why swing collapsed late-session
  (`n_pivots_today` decreased intraday, `minutes_since_pivot` pinned at 299).
- (Clarified scope: prior_day anchor and price_levels `dist_from_high` are NOT buffer-depth — those are
  HIGH-DAILYLOAD and the restart-wipe/warm-start facets. swing is the one genuine session-cumulative group.)

**Decision: `DEFAULT_BUFFER_MINUTES` 300 → 750** (720m session + 30m slack).
- Cost (`profile 1250 720 --latest`): per-group-summed live recompute **~15.6s/shard** at 1250 tickers vs
  the **60s** minute budget — comfortable (the deployed BATCHED path is lower; reduction groups share one
  marshal). The current live `group_compute_p99` is 2.24s at 300m.
- The reduce (universe-wide) ring is capped **independently** (`sharded_capture.reduce_buffer_minutes` =
  cross_sectional_rank's window + slack), so this only deepens the per-shard MAP ring where swing lives.
- **Parity-neutral** for every ≤240m-window group (deeper buffer = strictly more backfill-equivalent
  context); **parity-improving** for swing.

## Also (maintenance): fixed trunk-red `test_fp_stream_sim`
The CRITICAL-1 `resolve_points` refactor made reduction assemble paths SELECT precomputed `__pt_<name>`
columns (resolved over the whole buffer, gap-safe lag). The deployed `compute_reduction_batch` calls
`resolve_points` internally, but `stream_sim.process_stream_minute` built `latest_frame = ring.last_minutes(1)`
(raw single minute, null lag-points) and passed it to `emit_rust_unified`/`emit_numpy` → `ColumnNotFoundError
__pt_c`. Mirrored the deployed wiring (`resolve_points(engine.groups, frame, latest)`); also corrected the
companion test that fed `emit_numpy` a raw slice. Same incremental==batch parity assertion preserved.

## Verification
- `test_fp_latest` 38 pass, `test_fp_warm_start`, `test_fp_swing` pass; `test_fp_stream_sim` 3/3 pass.
- **Full suite: 298 passed, 27 skipped** (ignoring the 2 known pre-pivot stale modules). `py_compile` clean.
  (No `ruff` is installed in `fp-dev`/host/CI — used `py_compile` for the syntax gate.)

## Commits (on integration/converged via merge `9d479da`)
- `b0ce678` fix(stream_sim): resolve `__pt_*` point columns before reduction emit (trunk-red)
- `e2a98c2` fix(capture): size trailing buffer to the full session for cumulative groups (CRITICAL-2)
- Pre-deploy rollback point (trunk before this cycle): `91d485f`.

## Next
1. **Clean restart batch** (SEQUENCING RULE): `FP_WARM_START=1` + this buffer bump + the FIXED-CODE feature
   fixes → ONE restart; verify warm-start logs + freshness < 3 min + 8/8 shards; recompute contaminated data.
2. **Follow-up queued (backlog P1):** drop the global buffer recompute tax — the 750m depth makes windowed
   groups (momentum_run needs 60m, dominates cost) recompute history they don't use. Fix via per-group
   buffer-depth slicing or a stateful-swing accumulator (cleaner; removes the tax at the source). Neither
   blocks the clean restart.
