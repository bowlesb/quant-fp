# ⏵ CURRENT OPERATIONAL STATE — 2026-06-15 ~11:30 ET (read this block first)

**Live now:** parity-true capture running on the real Alpaca SIP feed. Trunk = `integration/converged`
(bind-mounted into the `feature-computer` container at `/app`; running process picks up code only on
`docker restart feature-computer`). Universe **11,336** symbols (widened from 3,070; alphabetical bias
removed). Stack: `quant-timescaledb-1` (db quant/quant, pw in .env), `quant-prometheus-1` (:9090),
`quant-grafana-1` (host **:3001**, dashboard uid `bar-to-vector-latency`). Creds in `.env` — never print.

**Autonomous operation (runs WITHOUT any interactive session — host cron):**
- `*/5` → `ops/healthcheck.sh --json` read-only tripwire → `~/.quant-healthcheck/healthcheck.jsonl`.
- `14,44 * * * *` → `ops/autonomous_loop.sh` → one headless `claude -p` cycle (maintain + advance one
  backlog item), logs in `~/.quant-loop/`. Reads `ops/autonomous_loop_prompt.txt`, this file,
  `docs/AUTONOMOUS_BACKLOG.md`, `docs/MAINTENANCE_PROTOCOL.md`. flock = no overlap; 25-min cap/cycle.
- Check it: `tail ~/.quant-loop/cycle-*.log`; latest `docs/progress/*`; `git log --oneline -15 integration/converged`.
- Pause it: `crontab -e`, comment the `autonomous_loop.sh` line.

**Owner-greenlit priorities (loop pursues in order — see `docs/AUTONOMOUS_BACKLOG.md`):**
1. **Per-symbol fast/tick path on the real feed** — sub-minute bet latency. The deployed bars-only batch
   path is ~60s bar→vector BY DESIGN (minute-close wait); feature compute itself is ~0.7s. **Big de-risk:**
   the incremental accumulator that makes this possible is ALREADY BUILT + PARITY-PROVEN —
   `quantlib/features/incremental.py` `WindowedSumState`, 0.49ms/minute, matches `quant_tick.windowed_sums`
   cell-for-cell (see "THE chosen design" + "Exact next steps" below). Remaining = integration into the live
   worker. Parity is sacred.
2. **Parity validation ledger live** — populate `feature_validation_day`/`feature_trust`, certify trust grades.
3. **Nightly re-seed + relaunch automation** — capture launched with a HARDCODED `2026-06-15` date arg;
   wire a nightly re-seed + relaunch for autonomous daily operation.

**Latency observability (this session):** dual metric `feature_vector_latency_seconds` (end-to-end) +
`feature_assemble_seconds` (last-bar) + per-ticker drill-down table `latency_slow_symbols` + Grafana.
The healthcheck `bar_to_vector_latency` FAIL (~60s) is the batch floor surfaced on purpose → fixed by #1.

**Resume this conversation:** `cd /home/ben/quant-fp && claude --resume` (or `--continue`). The loop runs
regardless. Dirty working-tree files (`experiments/dl_research/train.py`, `quantlib/features/backfill_bars.py`)
are earlier-agent leftovers, left intentionally uncommitted.

---

_The block below is the deep engine state from the pre-reboot fused/incremental-engine session. It is the
authoritative technical reference for priority #1 (the fast path). Keep it._

# Resume State — pick up exactly here after reboot

Written before a `sudo reboot` (to fix the GPU driver). Everything below is COMMITTED on branch
`fp-platform` (8 commits this session, tip `5555aaf`). Working tree clean. Nothing to recover from RAM.

## One-line status
Streaming feature platform proven end-to-end at 10k tickers; per-minute latency driven **4.9s → 0.9s
p99**; feature storage types + compaction done; next big effort is the **fused/declarative engine** (design
in `docs/FUSED_ENGINE.md`), with a **GPU-backfill** track unblocked by the reboot.

## First thing to do after reboot
1. Verify the GPU is back (the reboot was the fix — stale 535.288 module vs 535.309 libs):
   ```bash
   nvidia-smi                         # should show the RTX 3090, no NVML error
   cat /proc/driver/nvidia/version    # should read 535.309.01
   ```
2. The old `quant-*` compose stack was intentionally `docker compose down`'d (it was the pre-pivot trading
   stack; data volumes preserved — TimescaleDB is a bind mount at `~/quant/data/pg`). It is NOT needed for
   the feature-platform work. Leave it down unless you want the old dashboards.
3. The `fp-dev` image is the only thing the platform uses. Rebuild if needed: `make dev-image` (or
   `docker build -t fp-dev -f docker/fp-dev.Dockerfile .`). For the GPU spike, add `polars[gpu]` to it:
   `pip install "polars[gpu]" --extra-index-url=https://pypi.nvidia.com` (bake into the Dockerfile), and
   run the container with `--gpus all`.

## What was accomplished this session (all committed)
| commit | what |
|---|---|
| `555e615` | Feature **storage types** (Float32/UInt8/Int16, declared `storage=` field, read-upcast, zstd split), **per-minute append** + **compaction**, **Alpaca msgpack mock** + streaming proof |
| `6d72b52` | capture buffer kept as a DataFrame (dropped the per-minute list[dict] round-trip) — reader 4.9s→0.5s |
| `feeb50d` | **worker thread pinning** (fixed N×C-thread oversubscription) — 5.1s→1.9s |
| `5bb8e31` | shard default = `cpu_count//3` (~10 fat shards beat 30 thin ones) — 1.9s→1.55s |
| `fd4f6eb` | **multi_day daily-feature cache** (compute once/day, not per minute) — 1.55s→**0.90s** |
| `33111e5`,`5555aaf` | `docs/FUSED_ENGINE.md` design (+ modeling/backfill reshape) |
| `67a03d3` | `technical` fast `compute_latest` on Rust kernels (last heavy group converted) |

### Latency progression (10k × 519 features, full vector per minute, through the REAL StockDataStream)
4.9s → 1.9s (thread pinning) → 1.55s (shard tuning) → **0.90s p99** (multi_day cache). Budget is 60,000ms.
Optimal config measured: **10 shards × 3 polars threads** at 10k/32-cores.

## The three tracks
- **A — feature types: DONE.** Guide: `docs/FEATURE_TYPES.md`. ~54% smaller on disk; reads widen to Float64.
- **B — declarative/fused engine: BUILT incl. OLS (migration ongoing).** `quantlib/features/declarative.py`:
  a `ReductionGroup` declares `reduced()` (mean/std/sum) + `regressions()` (OLS slope/corr/r2/mean_y) +
  `points()` + `assemble()` ONCE → engine generates `compute()` (rolling backfill) + `compute_latest()`
  (at-T live), and `compute_reduction_batch()` runs MANY groups in ONE shared marshal+kernel pass (wired
  into `process_bars`). OLS is just six paired windowed sums, so it folds into the same batch. **Migrated
  (11): volume, volatility, ohlc_vol, trade_flow, quote_spread, return_dynamics, price_volume (70 feats),
  momentum, trend_quality, efficiency, distribution (skew/kurt from power sums — no extension).** Each
  parity-gated. 10k COMPUTE-only p99 (writes excluded) fell 902 (pre-declarative) → ~660ms as groups
  migrated. Remaining reduction-shaped: liquidity (autocovariance), price_levels (min/max), market_beta
  (SPY-join), price_returns (point-only, cheap). **Async writes evaluated and REJECTED** (background-thread zstd contends: 10k sync
  884ms vs async 981ms p99); `StoreWriter` is opt-in (`FP_ASYNC_WRITE`), default sync. For the bet-latency
  decoupling, REORDER (compute→bet→write sync) when a bet step exists, don't use a contending thread.
- **C — GPU: SPIKED AND RULED OUT for feature compute.** RTX 3090 works post-reboot; `fp-gpu` image built.
  But the polars GPU engine is SLOWER on our ops (transfer/launch overhead at 1–4M rows) and `rolling_*_by`
  (every backfill feature) is unsupported → CPU fallback. Parity was fine (1e-16…1e-13). CPU path wins +
  stays readable. 3090 → reserve for model TRAINING. Details in `docs/FUSED_ENGINE.md`.

## WHERE THE TIME ACTUALLY GOES (measured 2026-06-14, FP_PHASE_TIMING=1)
Decomposed the declarative batch at 1250 tickers × 60m, 11 groups = 138ms: **only 14% (18.9ms) is raw
Rust kernel compute. 86% is polars shuffling** — derive value columns **45%** (62ms), assemble pivot+join
**27%** (37ms), kernel prep/rebuild 9%, marshal ~0%. So the <100ms strategy is NOT incremental state (the
prior codebase re-scanned too and was fine) — it's **killing the shuffling**:
1. **Move derivation into the kernel** (biggest, 45%): the OLS cross-products (x·y, x², y²) and power-sums
   (r², r³, r⁴) are materialized as ~60 polars columns over 75k rows every minute. A kernel that takes the
   RAW x/y columns and forms the products *inside* its single backward pass eliminates the 62ms derive AND
   the rebuild. PARITY-SAFE: the kernel stays the shared primitive validated cell-for-cell vs the backfill
   polars form — the optimization sits UNDER the parity boundary, not beside it (no bolt-on parity layer).
2. **Collapse the assemble** (27%): one pivot (long→wide) for all of a group's canonical columns instead of
   one pivot+join per (col, stat); or have the kernel return wide.
Run the decomposition any time: instrument is gated by `FP_PHASE_TIMING=1` in `_phase.py`.

## THE chosen design (owner's direction): pre-prep between minutes, minimal compute AT the mark
"Set it up so work is pre-prepped, and the only time spent at the one-minute mark is the exact compute for
that moment, fastest way possible." Concretely — a **stateful incremental accumulator** per worker:
- Keep, in numpy, a **running per-(symbol, window, value-col) sum** + a rolling buffer of the **derived
  values** (close-products, OLS x/y/xy, power-sums) for the trailing window. These ARE the pre-prepped
  inputs (like the prior codebase's held arrays).
- **At minute T (the only work on the critical path):** compute the NEW minute's derived values (N symbols
  × ~40 cols — small), then for each window `running_sum[w] += derived[T] − derived[T−w]` (O(symbols ×
  windows × cols) ≈ ~1M float ops ≈ a few ms), then assemble features from the running sums. No re-scan of
  the 60-min buffer, no per-minute polars derive/pivot over 75k rows. That's the ~14%-is-real-compute gap
  closed → targets the few-ms / <100ms regime.
- **PARITY (sacred — the reason this platform exists):** the running sums are validated cell-for-cell vs
  the recompute by the SAME parity test; this sits UNDER the parity boundary (not a bolt-on framework like
  the old system). Incremental float sums drift, but per-trading-day the accumulation stays far inside
  tolerance; bound it with a **daily resync** (rebuild the accumulator from the buffer at session start,
  which also gives crash recovery). Backfill stays the polars rolling form (the truth); live becomes the
  accumulator; the test proves they agree.
- **STATUS: the accumulator is BUILT and PROVEN** (`quantlib/features/incremental.py`,
  `tests/test_fp_incremental.py`): `WindowedSumState` matches `quant_tick.windowed_sums` cell-for-cell, and
  measured **0.49ms/minute** at a shard's scale (1250×40 cols×10 windows) vs the 138ms recompute (~280×).
  So the uncertain part (is incremental parity-safe AND fast?) is RESOLVED.
- **REMAINING = integration** (engineering, de-risked): wire `WindowedSumState` into the live worker so the
  minute mark only (a) derives the NEW minute's ~40 value columns (one minute, not 75k rows), (b) folds it
  in (0.49ms), (c) assembles features from the running sums. Plus: seed/resync from the buffer at session
  start (drift bound + crash recovery), handle gaps (the expire loop already does — it's time-based) and
  symbols entering/leaving (index management), and keep the non-declarative groups. Backfill unchanged.

## (earlier framing) incremental windowed sums
The design recomputes every feature over the full 60-min buffer every minute — O(buffer×features). Because
the declarative engine routes EVERYTHING through windowed SUMS (mean/std/OLS all derived from sums) and sums
are associative, a running aggregate (add the new minute, subtract the minute that aged out of each window)
is BIT-EXACT reproducible by backfill — parity holds by construction. That turns per-minute work O(buffer)→
O(features), the ~5–7× toward <100ms. Build: a stateful Rust kernel keeping per-(symbol,window) running sums
across minutes (reconstructable from the buffer for crash recovery), feeding the same canonical columns the
declarative groups already assemble from. This is the big focused next effort; finish the migrations first
so the kernel covers all heavy groups. (Honest: 10k×519 over a 60-min buffer on 32 cores may floor at
~100–200ms under concurrency overhead, not necessarily 100ms flat.)

## Exact next steps (in order)
1. **Remaining reduction-shaped groups** (each parity-gated by `tests/test_fp_latest.py`): `liquidity`
   (mean/sum fit the engine, but roll-spread is an AUTOCOVARIance — needs a lag-product derived col or a
   small extension), `price_returns` (point-only: ret_w = close/close.shift(w) − 1 — migratable as all
   `points()`, but it's already cheap so low priority). The other heavy reduction/OLS groups are DONE.
2. **Two small engine extensions, then migrate their groups:**
   - **min/max** stat (rust_reductions already returns them; add `max_`/`min_` accessors + a windowed
     min/max kernel for the BATCH since windowed_sums can't do min/max) → `price_levels`.
   - **3rd/4th moments** (sum of r³, r⁴ + expose `n_`) → `distribution` (skew/kurt).
   - **a `prepare(frame)` hook** for groups whose x/y need a cross-symbol join (broadcast SPY return) →
     `market_beta` (regresses stock ret on SPY ret).
3. **Re-benchmark 10k** as groups migrate (current 6-group ~0.88s p99); the more migrate, the lower.
4. **Sharded backfill runner** — a new feature backfills months×10k across all cores "in minutes" (reuse
   live sharding; read from the store / raw bars on disk) — the modeling-iteration payoff.

## Key commands (all in ~/quant-fp)
```bash
# tests (the 2 stale modules predate the pivot — ignore them)
docker run --rm -v "$PWD":/app -w /app fp-dev python -m pytest tests/ -q \
  --ignore=tests/test_daily_closes_source.py --ignore=tests/test_features.py     # 156 pass

# per-group live profile at a shard's scale (find the next target)
docker run --rm -e POLARS_MAX_THREADS=3 -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.features.profile 1000 60 250 5 --latest

# end-to-end streaming benchmark through the REAL StockDataStream -> msgpack mock
#   args: <n_symbols> <n_shards> <measure_minutes> <warmup> <window>   (BENCH_ROOT must be a mounted dir)
docker run --rm -v /tmp/bh:/bh fp-dev sh -c 'rm -rf /bh/* /bh/.??*'   # clean (root-owned)
docker run --rm --memory=15g -e MOCK_INTERVAL_SEC=0 -e DB_PASSWORD=mock -e BENCH_ROOT=/bh \
  -v "$PWD":/app -v /tmp/bh:/bh -w /app fp-dev \
  python -u -m quantlib.features.bench_stream 10000 10 15 30 60        # ~0.9s p99
```

## Gotchas learned this session (so they aren't re-hit)
- **fork + polars deadlocks** the child → `run_sharded_capture` uses a **spawn** context.
- **Don't let workers default to all polars threads** (N workers × 32 = thrash) → POLARS_MAX_THREADS pinned.
- Bench `BENCH_ROOT` must be a **host-mounted** dir or files vanish with the container (and are root-owned —
  clean via a docker `rm`, not host `rm`).
- Mock must set `ping_interval=None` (the reader blocking the loop under flood otherwise drops the ws).
- The reader must not block the event loop past the ping timeout in production (offload heavy reduce later).
- `loaders.py` reads `DB_PASSWORD` at import — pass `-e DB_PASSWORD=mock` even when not using the DB.
