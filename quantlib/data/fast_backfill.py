"""Multiprocess, direct-httpx tick backfill engine — the high-throughput trades/quotes fetch path.

The alpaca-py SDK serializes tick parse on the GIL, so a single-process thread pool plateaus at ~31
symbol-days/min regardless of thread count. This engine distributes (symbol, day) UNITS across a PROCESS
pool (each worker runs its own thread pool of httpx fetches), getting download + columnar parse to run
truly in parallel — measured ~150-160 symbol-days/min, a ~5x lift.

It reuses the orchestrator's on-disk contract verbatim:
  * per-(symbol, date) partition layout  (``raw_backfill.partition_dir`` / ``write_partition``)
  * resumable append-only manifest parts  (``raw_backfill.manifest_dir`` / ``write_manifest_part``)
Only the FETCH+PARSE inner path changes (``fast_fetchers`` instead of the SDK). Each worker writes its OWN
manifest part files (pid-unique names), so the union on resume sees every prior fetch and re-running SKIPS
done symbol-days.

Units are single-symbol-day (NOT multi-day chunks): pagination is sequential WITHIN a symbol-day, so the
parallelism comes from running MANY symbol-days at once. Single-day units keep peak memory per in-flight
fetch bounded (one mega-cap day, not a multi-day chunk) — safe even for the heaviest quotes symbol-days.
"""

from __future__ import annotations

import datetime as dt
import logging
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

import httpx
import polars as pl

from quantlib.data.fast_fetchers import (
    fetch_quotes_day_fast,
    fetch_trades_day_fast,
    make_client,
)
from quantlib.data.raw_store import (
    load_manifest,
    resumable_done_keys,
    write_manifest_part,
    write_partition,
)

logger = logging.getLogger("fast_backfill")

# Mirror raw_backfill.SETTLE_WINDOW_DAYS: within this many days of the trading date an EMPTY manifest entry
# is reconsidered (re-fetched) because Alpaca's tick tape may not have settled when it was first fetched;
# past the window an empty entry is a genuine no-data symbol-day and stays done. The tick tiers (this engine)
# are exactly where the premature-empty poison was observed (06-18 SPY trades=2, AAPL/NVDA=0 while QQQ=757k).
SETTLE_WINDOW_DAYS = 5

DEFAULT_PROCESSES = 24
DEFAULT_THREADS_PER_PROCESS = 8
MANIFEST_FLUSH_EVERY = 500

# Mega-cap-aware quotes pass. Fetching one full-UTC-day SIP QUOTE for a megacap/ETF (SPY/QQQ/IWM/NVDA/
# AAPL/TSLA/...) has a TRANSIENT peak RSS of ~2.8 GB (measured: SPY 2026-04-21 = 4.1M rows, final frame
# 0.18 GB but ~2.8 GB peak during HTTP-buffer + JSON-parse + polars-construct, ~15x the final size).
# The default 16-way concurrency held ~16 such peaks at once (~45 GB) and OOM'd the 40 GB cap twice;
# lowering processes/threads alone failed because the peak is PER-UNIT, not per-process. So the heaviest
# `QUOTES_HEAVY_COUNT` symbols (the top of the dollar-volume ranking = the top of the quote-row-count
# ranking) run in a SEPARATE bounded-concurrency pass: HEAVY_PROCESSES x HEAVY_THREADS held peaks
# (~6 x 2.8 GB ~ 17 GB, comfortable under 40 GB); the long tail then runs at full concurrency (its
# per-unit frames are 10-100x smaller). Trades frames are ~10-50x smaller, so trades skips this pass.
QUOTES_HEAVY_COUNT = 60
QUOTES_HEAVY_PROCESSES = 6
QUOTES_HEAVY_THREADS = 1


@dataclass
class FetchResult:
    """Outcome of one (symbol, day) unit: rows + on-disk bytes (for the manifest)."""

    symbol: str
    date: str
    rows: int
    bytes: int


_WORKER_CLIENT: httpx.Client | None = None


def _worker_client() -> httpx.Client:
    """One pooled httpx client per WORKER PROCESS (lazily created), reused across that process's units."""
    global _WORKER_CLIENT
    if _WORKER_CLIENT is None:
        _WORKER_CLIENT = make_client()
    return _WORKER_CLIENT


def _fetch_write_unit(store: str, tier: str, symbol: str, day_iso: str) -> FetchResult:
    """Fetch+write ONE symbol-day using this process's pooled (thread-safe) httpx client."""
    day = dt.date.fromisoformat(day_iso)
    client = _worker_client()
    fetcher = fetch_trades_day_fast if tier == "trades" else fetch_quotes_day_fast
    frame = fetcher(client, symbol, day)
    size = write_partition(store, tier, symbol, day, frame)
    return FetchResult(symbol=symbol, date=day_iso, rows=frame.height, bytes=size)


def _fetch_batch(
    store: str, tier: str, units: list[tuple[str, str]], threads_per_process: int
) -> list[FetchResult]:
    """Worker: fetch+write a BATCH of symbol-days with an inner THREAD pool, so each process runs
    ``threads_per_process`` concurrent downloads. Download releases the GIL (so threads overlap on the
    network — the real bottleneck); the columnar parse is per-process serialized, which is fine because
    we are network-bound. A failed unit is skipped (unrecorded -> a resume retries it), never aborting
    the batch. httpx.Client is thread-safe and shares the process's connection pool across the threads.
    """
    results: list[FetchResult] = []
    with ThreadPoolExecutor(max_workers=threads_per_process) as pool:
        futures = {
            pool.submit(_fetch_write_unit, store, tier, symbol, day_iso): (
                symbol,
                day_iso,
            )
            for symbol, day_iso in units
        }
        for future in as_completed(futures):
            symbol, day_iso = futures[future]
            try:
                results.append(future.result())
            except (
                httpx.HTTPError,
                KeyError,
                OSError,
                pl.exceptions.PolarsError,
            ) as error:
                logger.warning(
                    "tier=%s %s %s: fetch failed, skipping: %s",
                    tier,
                    symbol,
                    day_iso,
                    error,
                )
    return results


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), max(1, size))]


def _utc_today() -> dt.date:
    """Today's UTC date — the reference for the rows-aware settle window. A seam for deterministic tests."""
    return dt.datetime.now(dt.timezone.utc).date()


def _pending_units(
    store: str, tier: str, symbols: list[str], days: list[dt.date]
) -> list[tuple[str, str]]:
    """(symbol, date_iso) units a resume must still fetch — ROWS-AWARE so a RECENT empty (premature/unsettled)
    entry is re-fetched, not stranded. resumable_done_keys skips real rows and aged-out empties; a 0-row entry
    within the settle window is treated as pending so the now-settled tape is acquired (the 06-18 poison)."""
    manifest = load_manifest(store, tier)
    done = resumable_done_keys(manifest, _utc_today(), SETTLE_WINDOW_DAYS)
    day_isos = [day.isoformat() for day in days]
    return [
        (symbol, day_iso)
        for symbol in symbols
        for day_iso in day_isos
        if (symbol, day_iso) not in done
    ]


@dataclass
class _TierTotals:
    """Running totals + manifest part-sequence shared across passes of one tier (so the heavy and tail
    quote passes append to ONE monotonic part-sequence and one combined count)."""

    written: int = 0
    bytes_written: int = 0
    part_seq: int = 0


def _run_units(
    store: str,
    tier: str,
    units: list[tuple[str, str]],
    processes: int,
    threads_per_process: int,
    total_units: int,
    totals: _TierTotals,
) -> None:
    """Fetch+write one set of (symbol, day) units across a PROCESS pool, accumulating into ``totals`` and
    flushing append-only manifest parts every ``MANIFEST_FLUSH_EVERY``. A dead worker (e.g. a mega-cap
    OOM under the cgroup cap) ends THIS pass cleanly — every unit not yet recorded is simply retried on
    resume. Shared by the heavy and tail quote passes and the single trades pass."""
    if not units:
        return
    buffer: list[dict] = []
    batches = _chunk(units, max(1, threads_per_process * 8))
    # SPAWN, not fork: this engine is launched from a parent that has already run thread pools (the bars
    # tier) and logs heavily, so a forked worker inherits the logging/alloc locks in a HELD state and
    # deadlocks on its first log call (observed: workers run ~1min then all freeze at 0% CPU). Spawn
    # starts each worker from a clean interpreter, so no parent lock is inherited.
    with ProcessPoolExecutor(
        max_workers=processes, mp_context=mp.get_context("spawn")
    ) as executor:
        futures = {
            executor.submit(
                _fetch_batch, store, tier, batch, threads_per_process
            ): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            try:
                results = future.result()
            except BrokenProcessPool:
                logger.error(
                    "tier=%s: worker pool broke (a worker process died); ending pass — resume re-runs the rest",
                    tier,
                )
                break
            for result in results:
                totals.written += 1
                totals.bytes_written += result.bytes
                buffer.append(
                    {
                        "tier": tier,
                        "symbol": result.symbol,
                        "date": result.date,
                        "rows": result.rows,
                        "bytes": result.bytes,
                        "fetched_at": dt.datetime.now(dt.timezone.utc),
                    }
                )
                if len(buffer) >= MANIFEST_FLUSH_EVERY:
                    totals.part_seq += 1
                    write_manifest_part(store, tier, buffer, totals.part_seq)
                    buffer = []
            logger.info(
                "tier=%s progress: %d/%d units, %.3fGB",
                tier,
                totals.written,
                total_units,
                totals.bytes_written / 1024**3,
            )
    if buffer:
        totals.part_seq += 1
        write_manifest_part(store, tier, buffer, totals.part_seq)


def run_tier_fast(
    store: str,
    tier: str,
    symbols: list[str],
    days: list[dt.date],
    processes: int = DEFAULT_PROCESSES,
    threads_per_process: int = DEFAULT_THREADS_PER_PROCESS,
    heavy_count: int = QUOTES_HEAVY_COUNT,
    heavy_processes: int = QUOTES_HEAVY_PROCESSES,
    heavy_threads: int = QUOTES_HEAVY_THREADS,
) -> tuple[int, int]:
    """Backfill one tick tier across a PROCESS pool. Returns (partitions_written, bytes_written).

    QUOTES are mega-cap-aware: the heaviest ``heavy_count`` symbols (assumed at the FRONT of ``symbols``,
    which the orchestrator passes in descending dollar-volume = descending quote-row-count) run FIRST in a
    low-concurrency pass (``heavy_processes`` x ``heavy_threads``) so only a bounded handful of multi-GB
    mega-cap day-frames coexist; the remaining tail then runs at full ``processes`` x ``threads_per_process``.
    Trades frames are ~10-50x smaller, so the trades tier skips the heavy pass (single full-concurrency pass).

    Each completed unit appends to an in-memory manifest buffer that flushes to an append-only part file
    every ``MANIFEST_FLUSH_EVERY`` — identical resume semantics to the thread-pool orchestrator.
    """
    if tier not in ("trades", "quotes"):
        raise ValueError(f"fast engine handles tick tiers only, got tier={tier!r}")

    heavy_symbols = symbols[:heavy_count] if tier == "quotes" else []
    tail_symbols = symbols[len(heavy_symbols) :]
    heavy_units = _pending_units(store, tier, heavy_symbols, days)
    tail_units = _pending_units(store, tier, tail_symbols, days)
    total_units = len(heavy_units) + len(tail_units)
    if total_units == 0:
        logger.info("tier=%s: nothing pending (all symbol-days already done)", tier)
        return 0, 0

    totals = _TierTotals()
    if heavy_units:
        logger.info(
            "tier=%s HEAVY pass: %d mega-cap symbol-days at %d procs x %d threads",
            tier,
            len(heavy_units),
            heavy_processes,
            heavy_threads,
        )
        _run_units(
            store,
            tier,
            heavy_units,
            heavy_processes,
            heavy_threads,
            total_units,
            totals,
        )
    if tail_units:
        logger.info(
            "tier=%s TAIL pass: %d symbol-days at %d procs x %d threads",
            tier,
            len(tail_units),
            processes,
            threads_per_process,
        )
        _run_units(
            store, tier, tail_units, processes, threads_per_process, total_units, totals
        )

    logger.info(
        "tier=%s done (%d partitions, %.3fGB, %d procs x %d threads tail, %d x %d heavy head of %d)",
        tier,
        totals.written,
        totals.bytes_written / 1024**3,
        processes,
        threads_per_process,
        heavy_processes,
        heavy_threads,
        len(heavy_symbols),
    )
    return totals.written, totals.bytes_written


def fetch_symbol_days_threaded(
    tier: str, symbol_days: list[tuple[str, dt.date]], threads: int
) -> dict[tuple[str, str], pl.DataFrame]:
    """In-process thread-pool fetch of many symbol-days (used by the per-process worker / benchmarks).

    Returns ``{(symbol, date_iso): frame}``. Kept separate from the process-pool path so a single
    process can be benchmarked or used when process startup overhead is not worth it (few units).
    """
    fetcher = fetch_trades_day_fast if tier == "trades" else fetch_quotes_day_fast
    results: dict[tuple[str, str], pl.DataFrame] = {}
    with make_client() as client:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {
                executor.submit(fetcher, client, symbol, day): (symbol, day.isoformat())
                for symbol, day in symbol_days
            }
            for future in as_completed(futures):
                key = futures[future]
                results[key] = future.result()
    return results
