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
  (9): volume, volatility, ohlc_vol, trade_flow, quote_spread, return_dynamics, price_volume (70 feats),
  momentum, trend_quality.** Each parity-gated; `tests/test_fp_declarative.py` proves batched==per-group for
  reduction+OLS. 10k per-shard p99 fell 902 (pre-declarative) → 815ms as groups migrated. **Async writes evaluated and REJECTED** (background-thread zstd contends: 10k sync
  884ms vs async 981ms p99); `StoreWriter` is opt-in (`FP_ASYNC_WRITE`), default sync. For the bet-latency
  decoupling, REORDER (compute→bet→write sync) when a bet step exists, don't use a contending thread.
- **C — GPU: SPIKED AND RULED OUT for feature compute.** RTX 3090 works post-reboot; `fp-gpu` image built.
  But the polars GPU engine is SLOWER on our ops (transfer/launch overhead at 1–4M rows) and `rolling_*_by`
  (every backfill feature) is unsupported → CPU fallback. Parity was fine (1e-16…1e-13). CPU path wins +
  stays readable. 3090 → reserve for model TRAINING. Details in `docs/FUSED_ENGINE.md`.

## THE lever for <100ms: incremental windowed sums (the user's target)
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
