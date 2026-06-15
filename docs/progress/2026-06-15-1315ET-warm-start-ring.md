# Progress — 2026-06-15 ~13:15 ET — CRITICAL-2 warm-start ring (increment) + CRITICAL-1 committed/merged

## FOR THE OWNER (action needed)
- **No live changes this cycle** — every code change is default-OFF and capture was NOT restarted. The live
  system is healthy (see Maintenance).
- **Two parity-affecting code changes are now committed + merged to `integration/converged`** (they take
  effect only on the next `docker restart feature-computer`, which is deliberately deferred per the
  SEQUENCING RULE):
  1. **CRITICAL-1 (points() lag parity break)** — `c419519`. This was marked `[x]` in the backlog but had
     been left **uncommitted in the working tree** by the prior cycle (the note claimed it was committed; it
     was not). I verified the tests (49 pass / 2 skip), committed it (`ad713e2`), and merged it.
  2. **CRITICAL-2 (restart wipes buffers) — first increment** — `dc0187e`. `warm_start_ring` rehydrates the
     trailing ring from settled session bars, behind `FP_WARM_START` (default OFF). Inert until flipped.
- **Decision still owed before CRITICAL-2 fully closes:** the wave-3 finding that `DEFAULT_BUFFER_MINUTES=300`
  < a 390m session. Growing the buffer raises the deployed batch path's per-minute cost at 10k scale →
  needs a `make fp-profile` at session depth before bumping. Noted in the backlog CRITICAL-2 item.

## Maintenance
`healthcheck`: **11 PASS / 3 WARN / 1 FAIL** (unchanged across the cycle; no deploy). Freshness 2.3 min,
8/8 shards UP, universe 11,336, coverage 87.4%, alpha-bias 32.2%, OHLC invariants clean. The 1 FAIL
(`bar_to_vector_latency` ~60s) is the known batch-path floor (fixed by P1 #1). WARNs: validation ledger /
trust grades empty (known), `group_compute_p99` 2.07s (batch path, slightly slow). No safe-fix triggered.

## What I advanced
**P1.0 CRITICAL-2 (capture restart warm-start) — the keystone** (engine/store lane). Built
`capture.warm_start_ring(state, bars, depth, project_columns)`: on startup it rehydrates `state.ring` from
the session's already-settled bars BEFORE the first live minute. The source is `backfill_bars(day, symbols)`
— Alpaca historical **RAW**, i.e. the SAME unadjusted SIP tape the live stream delivers — so the seed is
**parity-true**: the warmed ring holds exactly the rows the live path would itself have accumulated, and the
first live minute computes features identical to a capture that was never restarted.

Wired into BOTH launch paths behind **`FP_WARM_START` (default OFF**, mirroring `FP_INCREMENTAL`):
- `run_capture` (single-process) — warm-start before `stream.run()`.
- `run_sharded_capture` — each worker warm-starts its shard's **map** ring (its owned symbols + index ETFs);
  the reader warm-starts the universe-wide **reduce** ring (projected + depth-capped exactly as
  `process_reduce` keeps it live).
With the flag unset the launch is byte-identical to today's cold start — **zero deploy risk**.

### Verification
- `tests/test_fp_warm_start.py` — **6 pass**, incl. THE parity gate `test_warm_start_then_live_minute_matches_cold`
  (warm-start through T-1 + process live minute T == a cold capture that streamed every minute 0..T), plus
  cold-build row-set equality, depth-cap eviction, empty-source no-op, and column projection.
- **20 pass** across `test_fp_warm_start` + `test_fp_sharding` + `test_sharding` + `test_fp_incremental_capture`
  (the sharded launch + incremental paths are unaffected).
- py-compile clean on all 3 changed modules. (ruff still not in the `fp-dev` image — flagged again.)

## Next step (next cycle — close out CRITICAL-2)
1. **Resolve the buffer-depth question** (wave-3 facet): `make fp-profile` the batch path at ~410m session
   depth vs 300m to size the per-minute cost; then either bump `DEFAULT_BUFFER_MINUTES` or make the buffer
   session-anchored for the cumulative groups only.
2. **The ONE clean restart:** batch CRITICAL-1 + the other FIXED-CODE feature fixes, set `FP_WARM_START=1`,
   restart capture, verify worker logs `warm-started ring: N minutes` + freshness < 3 min + 8/8 shards.
3. **Recompute** today's contaminated long-window + lag-point `source=stream` data via the backfill
   `compute()` path (immune to the reset).

After CRITICAL-2 closes, the next P1.0 items are CRITICAL-3 (breadth per-shard) and MED-DEDUP (duplicate
broadcast-symbol rows) — both engine/capture lane. P1 #1 (fast path) remains gated on the OPEN slice
constraint + the incremental r2 conditioning caveat (both documented).

## Leftovers (unchanged, intentional)
`experiments/dl_research/{train.py,results.json}` (modeller lane) and `quantlib/features/backfill_bars.py`
(a sound but separately-scoped Alpaca transient-400 retry fix from an earlier agent) remain uncommitted per
RESUME_STATE. The backfill retry would actually HELP the warm-start startup fetch — worth committing in a
future cycle.
