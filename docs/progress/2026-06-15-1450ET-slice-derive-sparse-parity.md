# 2026-06-15 ~14:50 ET — slice-derive sparse-symbol parity (P1 #1 increment)

## FOR THE OWNER (action items, top)
- **No live action required this cycle.** Change is library-only, behind `FP_INCREMENTAL_SLICE`
  (default OFF) and `FP_INCREMENTAL` (default OFF); the running capture is byte-identical until you
  flip a flag at a restart. Nothing deployed.
- **Still queued for your supervised clean restart** (unchanged from prior cycles, per the SEQUENCING
  RULE — a capture redeploy ESCALATES, I do not auto-do it): ship CRITICAL-1/2/3 feature fixes +
  flip `FP_WARM_START=1`, then recollect contaminated `source=stream` data from backfill.
- **Tooling gap:** `ruff` is NOT installed in the `fp-dev` image, so the CLAUDE.md "ruff check on
  changed files" step can't run in-container. I substituted `python -m py_compile` (clean) + the full
  parity suite. Worth baking `ruff` into `docker/fp-dev.Dockerfile` so the lint gate is real.

## Maintenance (healthcheck)
`docker exec feature-computer python -m quantlib.ops.healthcheck` → **11 PASS / 3 WARN / 1 FAIL**, all
known/by-design:
- Collection healthy: newest minute 2.2 min old, 8/8 shards UP, coverage 89.6% (10158/11336),
  per-minute active 3979, alphabetical-bias 31.8% (unbiased), 0 OHLC violations, 0 constant features.
- FAIL `bar_to_vector_latency` = 60s — the by-design batch minute-close floor (the target of P1 #1).
- WARN `validation_freshness` / `trust_grades` (ledger empty — greenlit #2) + `group_compute_p99` 2.5s.
No safe-fix needed (nothing dead/wedged, universe non-empty).

## Advanced (P1 #1 — the fast path)
Discovered the backlog "NEXT STEP" (wire `IncrementalEngine` behind `FP_INCREMENTAL`) was **already
done** (`5479fd0`, incl. the `FP_INCREMENTAL_PARITY` self-check → Prometheus); the backlog was stale and
is now corrected. The real remaining blocker was the **OPEN PARITY CONSTRAINT** on the fast SLICE path —
resolved this cycle.

**Problem:** the incremental slice-derive selected the new minute's value columns over a fixed
`DERIVE_SLICE=6`-minute window. But backfill's lag is **positional** — `close.shift(k).over("symbol")`
is the k-th prior **ROW**, however far back in time. A symbol that skips minutes (present at T, prior bar
>6 min back) had its short-lag columns slice-derived as a wrong **null**, where the whole-buffer/backfill
derive returns a real value → **live ≠ backfill**. This is why the slice path was gated "do not ship live."

**Fix (`806d624` / merge `2eb033c`):** `IncrementalEngine._matrix_at` now tails each present symbol's
last `max_lag+1` ROWS (minute-sorted `group_by("symbol").tail`) instead of a minute window. Positional
lags need exactly those rows, so the slice is **cell-for-cell identical to the whole-buffer derive** at
the latest row for dense AND sparse symbols. The expensive derive still runs on ~`max_lag+1` rows/symbol
(not the whole buffer) — the latency win is preserved. `max_lag` captured from `lag_specs` in `__init__`.

**Parity gate:** `tests/test_fp_incremental_features.py` adds a sparse-symbol stream (one symbol printing
every 10 min) and steps two engines — one slicing, one whole-buffer — asserting their assembled features
agree every minute. **Verified the guard BITES** under the old slice (`price_volume.up_volume_ratio_3m`
diverges at the sparse print minutes); clean under the row tail. Full incremental + parity suite **58
pass, 2 skip**. Docstrings in `incremental.py` / `capture.py` / `stream_sim.py` updated to the row-tail
semantics (the stale "time-based filter" rationale removed).

## Next
1. **Live A/B**: at the supervised clean restart, set `FP_INCREMENTAL_PARITY=1` (computes both, writes
   batch, records divergence metric); watch it run clean; later `FP_INCREMENTAL_SLICE=1`; then flip
   `FP_INCREMENTAL=1` to make the incremental path the source of truth.
2. **Zero-scan fold**: maintain the per-symbol last-`max_lag+1`-rows tail as engine STATE
   (O(active symbols)/min, no buffer materialize/sort) — the final per-minute cost removal, parity-gated
   like this row tail.
3. **Emit-on-arrival**: emit a symbol's vector as its bar arrives, not at minute close — the actual
   sub-minute bet-latency win (the headline goal of #1).
