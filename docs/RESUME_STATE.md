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
- **B — declarative/fused engine: BUILT (core done; migration ongoing).** `quantlib/features/declarative.py`:
  a `ReductionGroup` declares `reduced()`/`points()`/`assemble()` ONCE → engine generates `compute()`
  (rolling backfill) + `compute_latest()` (at-T live). `compute_reduction_batch()` runs MANY declarative
  groups in ONE shared marshal+kernel pass and IS WIRED INTO `process_bars` (the live path). Migrated so far:
  **volume, volatility, ohlc_vol** (each parity-gated; `tests/test_fp_declarative.py`). Batching speedup
  scales with group count: 1.69× (2 groups) → 1.88× (3) toward the one-marshal-vs-N limit.
- **C — GPU: SPIKED AND RULED OUT for feature compute.** RTX 3090 works post-reboot; `fp-gpu` image built.
  But the polars GPU engine is SLOWER on our ops (transfer/launch overhead at 1–4M rows) and `rolling_*_by`
  (every backfill feature) is unsupported → CPU fallback. Parity was fine (1e-16…1e-13). CPU path wins +
  stays readable. 3090 → reserve for model TRAINING. Details in `docs/FUSED_ENGINE.md`.

## Exact next steps (in order)
1. **Migrate the remaining pure mean/std/sum reduction groups** to `ReductionGroup` (mechanical, each
   parity-gated by `tests/test_fp_latest.py`): candidates `trade_flow`, `quote_spread`, `liquidity`,
   `price_levels`, `trend_quality`, `momentum`, `price_returns`. Each one joins the batch → bigger speedup.
2. **Extend the engine for two more reduction shapes**, then migrate the groups that need them:
   - OLS/correlation (sum of x, y, xy, x², y²) → `price_volume`, `market_beta`, `return_dynamics`,
     `trend_quality` (R²). Add a `corr_`/`slope_` accessor backed by `windowed_sums`.
   - 3rd/4th moments (sum of r³, r⁴ + an `n_` accessor) → `distribution` (skew/kurt).
3. **Re-benchmark the full 10k streaming run** once most groups are declarative — expect the per-shard
   compute to drop well below the current ~0.9s p99 (one marshal for ~12 groups instead of 12).
4. **Sharded backfill runner** so a new feature backfills over months×10k across all cores "in minutes"
   (reuse the live sharding infra; reads from the store / raw bars on disk) — the modeling-iteration payoff.

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
