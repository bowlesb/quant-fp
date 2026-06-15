"""Per-symbol latency drill-down — answers "WHICH tickers were too slow this minute" without exploding
Prometheus cardinality (11k symbols x per-minute series would be unbounded).

The reader stamps each symbol's bar-arrival wall-clock in ``on_bar``; the worker, once its shard vector
for the minute is assembled, computes per-symbol latency and picks the TOP-K slowest symbols, then
best-effort writes those K rows to TimescaleDB (``latency_slow_symbols``). The pure top-K selection
(``top_k_slow_symbols``) has NO I/O so it is unit-tested directly; ``write_slow_symbols`` is the thin,
fault-isolated psycopg write that MUST NOT crash or stall the capture hot path.

Two per-row numbers separate the cause of slowness:
- ``arrival_lag_s`` = symbol-bar-arrival - minute-boundary  → how late ALPACA delivered that bar.
- ``total_latency_s`` = vector-ready - symbol-bar-arrival   → our end-to-end for that symbol.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import psycopg

logger = logging.getLogger("latency_drilldown")

# Bounded per (shard, minute): we keep only the K slowest symbols so the DB write is one small
# executemany (K rows), never the whole 11k-symbol universe.
TOP_K_SLOW_SYMBOLS = 20

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

_INSERT_SQL = """
INSERT INTO latency_slow_symbols (minute, shard, symbol, arrival_lag_s, total_latency_s)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (minute, shard, symbol) DO UPDATE
SET arrival_lag_s = EXCLUDED.arrival_lag_s,
    total_latency_s = EXCLUDED.total_latency_s,
    captured_at = now()
"""


@dataclass(frozen=True)
class SlowSymbol:
    """One per-symbol latency row for the drill-down table."""

    symbol: str
    arrival_lag_s: float
    total_latency_s: float


def top_k_slow_symbols(
    symbol_arrivals: dict[str, float],
    ready_wallclock: float,
    minute_boundary_epoch: float,
    k: int = TOP_K_SLOW_SYMBOLS,
) -> list[SlowSymbol]:
    """Pick the ``k`` symbols with the highest ``total_latency_s`` (vector-ready minus that symbol's
    bar-arrival) for one (shard, minute). PURE — no I/O.

    ``symbol_arrivals`` maps symbol -> the wall-clock (``time.time()``) at which that symbol's bar for
    the minute arrived off the websocket. ``ready_wallclock`` is the wall-clock the shard vector finished
    assembling. ``minute_boundary_epoch`` is the minute's UTC boundary as a POSIX timestamp, used to
    derive ``arrival_lag_s`` (how late Alpaca delivered each bar). Ties are broken by symbol for a
    deterministic result.
    """
    rows = [
        SlowSymbol(
            symbol=symbol,
            arrival_lag_s=arrival - minute_boundary_epoch,
            total_latency_s=ready_wallclock - arrival,
        )
        for symbol, arrival in symbol_arrivals.items()
    ]
    rows.sort(key=lambda row: (-row.total_latency_s, row.symbol))
    return rows[:k]


def write_slow_symbols(minute: datetime, shard: int, rows: list[SlowSymbol]) -> None:
    """Best-effort write of the top-K slow rows for one (shard, minute) to ``latency_slow_symbols``.

    DELIBERATELY fault-isolated and OFF the hot path: any DB error is logged at WARNING and swallowed so a
    transient DB hiccup can NEVER crash or stall capture. One small ``executemany`` (K rows). Catches only
    the psycopg error families (a programming error like a bad arg list still raises)."""
    if not rows:
        return
    params = [
        (minute, shard, row.symbol, row.arrival_lag_s, row.total_latency_s) for row in rows
    ]
    try:
        with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, params)
    except (psycopg.OperationalError, psycopg.DatabaseError) as exc:
        logger.warning("latency_slow_symbols write skipped (shard=%s minute=%s): %s", shard, minute, exc)
