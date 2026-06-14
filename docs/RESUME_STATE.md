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
- **A — feature types: DONE.** Guide: `docs/FEATURE_TYPES.md`. Tests: `tests/test_fp_storage_dtype.py`,
  `tests/test_fp_compaction.py`. ~54% smaller on disk; reads widen back to Float64 (transparent).
- **B — fused/declarative engine: DESIGNED, NOT YET BUILT.** See `docs/FUSED_ENGINE.md`. The reshaped plan
  (per the modeling constraint): a feature DECLARES its windowed reduction ONCE → engine generates both the
  backfill (rolling) and live (at-T) forms → parity by construction + half the code. Two tiers (declarative
  fast/batched + arbitrary-polars escape hatch). Backfill runs on the SAME sharded engine as live (fast
  modeling iteration). Build it **additively** (new opt-in base class; existing groups untouched; migrate
  reduction-shaped groups one at a time, each parity-gated). This collapses the ~12× per-group marshaling
  (the current floor — groups are marshal-bound, not compute-bound).
- **C — GPU: diagnosed, unblocked by reboot.** RTX 3090 (Ampere, cc 8.6). After reboot + `polars[gpu]`,
  same polars code runs via `.collect(engine="gpu")`. Its home is **backfill** (truth-defining + huge
  batch; parity only checked live-CPU vs backfill-GPU within tolerance). Caveat: GPU engine falls back to
  CPU per-node for unsupported ops — benchmark the real feature graph, don't assume speedup.

## Exact next steps (in order)
1. **GPU verify** (above), then bake `polars[gpu]` into `fp-dev` and confirm `pl.LazyFrame(...).collect(engine="gpu")` works on a toy feature.
2. **Build the declarative reduction core** (`quantlib/features/declarative.py`, additive): a base that, from
   one reduction declaration, generates `compute()` (rolling) + `compute_latest()` (at-T via the existing
   `rust_windowed_sums`/`rust_reductions`). Unit-test that generated-rolling == generated-at-T (parity by
   construction). Then migrate ONE simple reduction group (e.g. `volume`) as proof; the generic parity test
   `tests/test_fp_latest.py` validates it.
3. **Batch the kernel pass** (Stage 2 in FUSED_ENGINE.md): collect all declarative groups' derived columns,
   one marshal + one `windowed_sums` over all columns, distribute back. Target per-shard ~300ms→~100ms.
4. **Sharded backfill runner** so a new feature backfills over months×10k across all cores "in minutes"
   (reuse the live sharding infra; reads from the store / raw bars on disk).
5. **GPU backfill spike**: run the backfill compute via `engine="gpu"`, measure speed + the live-CPU vs
   backfill-GPU parity delta against the declared tolerances.

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
