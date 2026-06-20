# Monday Collect-and-Save Plan — real-time capture without read/write contention

> **ARCHIVED 2026-06-20 — historical reference only.** This was the one-time forward-looking plan
> (authored 2026-06-13) for the FIRST live market-open capture milestone (definition-of-done #6/#7).
> Live sharded capture has been running daily for over a week; the current operational reality lives in
> `docs/OPERATIONS.md` (cron registry + relaunch), `docs/SESSION_WARMUP.md` (warm-start), the live state
> in `~/.quant-ops/SYSTEM_LOG.md` (the per-Monday plan is now Lead-maintained there), and the T+1
> backfill==live verification is `docs/PARITY_PLAYBOOK.md` / `docs/PARITY_COVERAGE.md`. The
> partition-disjoint, no-contention write design described below is implemented and proven in
> `tests/test_fp_sharding.py`. Kept for history; NOT the current design of record.

The plan for market open: capture every ticker every minute, compute all features, and **save them to
the feature store without DB read/write slowdowns or concurrency issues** (definition-of-done #6), so
that on Tuesday the backfill can be diffed against what we collected live (#7).

## What runs (one box, 24/7)
1. **Ingestor (one websocket).** Alpaca allows ONE market-data websocket per account. A single reader
   process owns it, receives bars for the WHOLE universe + trades/quotes for the OFI tier, and does the
   cheap work only (receive + route). It writes every raw bar to `bars_1m`, raw trades to `trades_raw`,
   quotes to the quote tables. (`services/ingestor`.)
2. **Sharded feature capture** (`real_capture.run_sharded_capture`). N = cores−2 persistent worker
   processes, each owns `hash(symbol) % N` — your model: **different processes watch different tickers.**
   Each worker holds its own trailing buffer for its symbols, computes the latest-minute feature vector
   (`compute_latest`, aggregate-at-T / Rust kernels), and writes ONLY its symbols.

## Save path — why there is no write contention
- **Feature store = Parquet, partition-disjoint, per-shard files.** Layout
  `group=<g>/v=<ver>/source=stream/date=<d>/data-<shard>.parquet`. Each worker writes its OWN file via a
  per-file temp + atomic `os.replace` (POSIX-atomic, same directory). N workers writing the same
  (group, date) partition **never touch the same file and never lock** — proven in
  `tests/test_fp_sharding.py::test_store_concurrent_shard_writes_do_not_clobber`. Reads glob
  `data*.parquet` and return the union. No database, no row locks, no fsync barrier shared across workers.
- **Raw ticks/bars = TimescaleDB**, written by the ingestor reader (one writer, batched), NOT by the
  feature workers — so feature compute and raw persistence don't contend. Bars are a light, single-place
  write (`bars_1m`); trades/quotes are batched inserts on the OFI tier only. The hypertable is
  segment-compressed by symbol; writes are append-mostly to the current chunk.
- **Idempotent.** Re-running a (group, source, date, shard) overwrites just that file; raw upserts are
  `ON CONFLICT (symbol, ts, source)`. A reconnect/replay self-corrects.

## Mode + universe
- Store root is tagged `real` (vs the `mock` simulation root) so simulated data can never land in the
  real store (`store.store_mode`).
- Universe = `tradable_universe()` (~13k active US equities); the per-minute cross-sectional rank is
  pinned to the day's `universe_membership` snapshot so live and backfill rank the identical set (#3).

## Pre-open checklist (must all be green)
- [ ] `make dev-image` current; `make test-fp` green (incl. parity + Rust + sharding + store-concurrency).
- [ ] Simulation dry-run: mock stream → sharded capture → mock store, features land (req #2).
- [ ] Real intake verified: Alpaca delivering bars (all), trades + quotes (OFI tier) — req #3.
- [ ] Latency: `make fp-bench` / `e2e_bench` p99 ≤ budget at 10k sharded (req #4).
- [ ] Disk headroom for the day's raw ticks + feature parquet (R11 floor).
- [ ] Store mode = `real`; capture writes `source=stream`.

## During the session
- Workers run continuously; each minute every shard writes its `data-<shard>.parquet`. The dashboard
  reads `CaptureState.group_timings` (per-group live latency) so a slow feature is visible immediately
  (req #5).

## Tuesday (T+1) — verify backfill == live (req #7)
After the historical API settles Monday: `make parity DAY=<monday>` recomputes every feature from the
settled backfill inputs AND the live-captured inputs through the IDENTICAL group code and diffs per the
declared `parity_method`/tolerance. `parity coverage` separately checks for capture gaps (cells the
backfill has that we didn't capture live). Target ≥95% per-feature parity; investigate any feature below.
See `PARITY_PLAYBOOK.md §7`.
