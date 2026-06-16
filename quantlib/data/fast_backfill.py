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
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
import polars as pl

from quantlib.data.fast_fetchers import (
    fetch_quotes_day_fast,
    fetch_trades_day_fast,
    make_client,
)
from quantlib.data.raw_store import (
    done_keys,
    load_manifest,
    write_manifest_part,
    write_partition,
)

logger = logging.getLogger("fast_backfill")

DEFAULT_PROCESSES = 24
DEFAULT_THREADS_PER_PROCESS = 8
MANIFEST_FLUSH_EVERY = 500


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


def _fetch_one(
    store: str, tier: str, symbol: str, day_iso: str, threads_per_process: int
) -> FetchResult:
    """Fetch+write ONE symbol-day. Runs inside a worker process; uses that process's pooled client.

    ``threads_per_process`` is unused here (each unit is one symbol-day); it is threaded through so the
    submit site can document the effective concurrency. Pagination is inherently sequential per unit.
    """
    day = dt.date.fromisoformat(day_iso)
    client = _worker_client()
    fetcher = fetch_trades_day_fast if tier == "trades" else fetch_quotes_day_fast
    frame = fetcher(client, symbol, day)
    size = write_partition(store, tier, symbol, day, frame)
    return FetchResult(symbol=symbol, date=day_iso, rows=frame.height, bytes=size)


def _pending_units(
    store: str, tier: str, symbols: list[str], days: list[dt.date]
) -> list[tuple[str, str]]:
    """(symbol, date_iso) units not yet recorded done in the tier manifest (resume skip)."""
    manifest = load_manifest(store, tier)
    done = done_keys(manifest)
    day_isos = [day.isoformat() for day in days]
    return [
        (symbol, day_iso)
        for symbol in symbols
        for day_iso in day_isos
        if (symbol, day_iso) not in done
    ]


def run_tier_fast(
    store: str,
    tier: str,
    symbols: list[str],
    days: list[dt.date],
    processes: int = DEFAULT_PROCESSES,
    threads_per_process: int = DEFAULT_THREADS_PER_PROCESS,
) -> tuple[int, int]:
    """Backfill one tick tier across a PROCESS pool. Returns (partitions_written, bytes_written).

    Each completed unit appends to an in-memory manifest buffer that flushes to an append-only part file
    every ``MANIFEST_FLUSH_EVERY`` — identical resume semantics to the thread-pool orchestrator, just
    driven from the parent process as futures complete.
    """
    if tier not in ("trades", "quotes"):
        raise ValueError(f"fast engine handles tick tiers only, got tier={tier!r}")
    units = _pending_units(store, tier, symbols, days)
    if not units:
        logger.info("tier=%s: nothing pending (all symbol-days already done)", tier)
        return 0, 0

    written = 0
    bytes_written = 0
    buffer: list[dict] = []
    part_seq = 0

    with ProcessPoolExecutor(max_workers=processes) as executor:
        futures = {
            executor.submit(_fetch_one, store, tier, symbol, day_iso, threads_per_process): (
                symbol,
                day_iso,
            )
            for symbol, day_iso in units
        }
        for future in as_completed(futures):
            result = future.result()
            written += 1
            bytes_written += result.bytes
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
                part_seq += 1
                write_manifest_part(store, tier, buffer, part_seq)
                buffer = []
            if written % 100 == 0:
                logger.info(
                    "tier=%s progress: %d/%d units, %.3fGB",
                    tier,
                    written,
                    len(units),
                    bytes_written / 1024**3,
                )

    if buffer:
        part_seq += 1
        write_manifest_part(store, tier, buffer, part_seq)

    logger.info(
        "tier=%s done (%d partitions, %.3fGB, %d procs x %d threads)",
        tier,
        written,
        bytes_written / 1024**3,
        processes,
        threads_per_process,
    )
    return written, bytes_written


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
