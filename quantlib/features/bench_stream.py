"""End-to-end streaming latency benchmark — the REAL ``alpaca-py`` ``StockDataStream`` against the
protocol-faithful msgpack mock, i.e. the EXACT Monday capture path with one env var flipped.

This is the proof for the platform's pre-Monday bar: ``real_capture.run_sharded_capture`` (the same code
that will run against the live Alpaca feed) is pointed at ``mock_stream.alpaca_server`` via
``STREAM_URL_OVERRIDE`` and fed N synthetic tickers/minute. Each shard records its per-minute compute+write
time (FP_BENCH_LOG); the authoritative per-minute latency is the SLOWEST shard that minute (shards run
concurrently, one core each), reported p50/p99/max over the full-buffer minutes. Synthetic reference/daily
snapshots are injected so it runs standalone (no DB / no Alpaca history).

Usage:  python -m quantlib.features.bench_stream <n_symbols> <n_shards> <measure_minutes> [warmup] [window]
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import multiprocessing as mp
import os
import statistics
import sys
import time
from pathlib import Path

import polars as pl
import websockets

from mock_stream.alpaca_server import make_handler
from quantlib.features.real_capture import run_sharded_capture
from quantlib.features.sharded_capture import INDEX_SYMBOLS

PORT = 9101
SESSION_DAY = "2026-06-15"
SECTORS = ("Technology", "Financials", "Health Care", "Energy", "Industrials", "Utilities",
           "Materials", "Consumer Staples", "Consumer Discretionary", "Real Estate", "Communication Services")


def synth_symbols(n: int) -> list[str]:
    """N synthetic tickers plus the index ETFs the market-context groups need replicated per shard."""
    return [f"T{i:05d}" for i in range(n)] + list(INDEX_SYMBOLS)


def synth_reference(symbols: list[str]) -> pl.DataFrame:
    """A per-symbol sector + tradability snapshot (round-robin sectors) matching REFERENCE_SCHEMA."""
    return pl.DataFrame(
        {
            "symbol": symbols,
            "sector": [SECTORS[i % len(SECTORS)] for i in range(len(symbols))],
            "shortable": [True] * len(symbols),
            "easy_to_borrow": [True] * len(symbols),
            "marginable": [True] * len(symbols),
            "fractionable": [True] * len(symbols),
        },
        schema={"symbol": pl.String, "sector": pl.String, "shortable": pl.Boolean,
                "easy_to_borrow": pl.Boolean, "marginable": pl.Boolean, "fractionable": pl.Boolean},
    )


def synth_daily(symbols: list[str], day: str, lookback: int = 260) -> pl.DataFrame:
    """Synthetic split-adjusted daily history (lookback trading days) for the multi-day groups."""
    end = dt.date.fromisoformat(day)
    dates = [end - dt.timedelta(days=offset) for offset in range(lookback, 0, -1)]
    rows = {"symbol": [], "date": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "vwap": []}
    for index, symbol in enumerate(symbols):
        base = 50.0 + (index % 500)
        for day_index, date in enumerate(dates):
            close = base + day_index * 0.05
            rows["symbol"].append(symbol)
            rows["date"].append(date)
            rows["open"].append(close - 0.2)
            rows["high"].append(close + 0.3)
            rows["low"].append(close - 0.3)
            rows["close"].append(close)
            rows["volume"].append(1_000_000.0)
            rows["vwap"].append(close)
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def _serve_mock(minutes: int, interval: float) -> None:
    async def serve() -> None:
        async with websockets.serve(make_handler(minutes, interval), "127.0.0.1", PORT,
                                    max_size=2**24, ping_interval=None):
            await asyncio.Future()

    asyncio.run(serve())


def _start_mock(minutes: int) -> mp.Process:
    """Run the mock in its OWN process — NOT a thread — so the parent stays single-threaded when
    run_sharded_capture forks its workers (forking a process with a live thread can deadlock the children)."""
    interval = float(os.environ.get("MOCK_INTERVAL_SEC", "0"))
    proc = mp.Process(target=_serve_mock, args=(minutes, interval), daemon=True)
    proc.start()
    return proc


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[rank]


def _report(root: str, n_symbols: int, n_shards: int, warmup: int) -> None:
    bench = Path(root) / "_bench"
    per_minute: dict[str, list[float]] = {}
    for shard_file in sorted(bench.glob("shard-*.jsonl")):
        for line in shard_file.read_text().splitlines():
            record = json.loads(line)
            per_minute.setdefault(record["minute"], []).append(record["ms"])
    minutes_sorted = sorted(per_minute)
    critical = [max(per_minute[minute]) for minute in minutes_sorted]  # slowest shard each minute
    warm = critical[warmup:] or critical  # measure on full-buffer minutes only
    reader_file = bench / "reader.jsonl"
    reader = [json.loads(line)["ms"] for line in reader_file.read_text().splitlines()] if reader_file.exists() else []
    reader_warm = reader[warmup:] or reader

    print(f"\n=== STREAMING latency: {n_symbols} symbols, {n_shards} shards (~{n_symbols // n_shards}/shard), "
          f"{len(minutes_sorted)} minutes ({len(warm)} measured post-warmup) ===")
    print(f"per-minute CRITICAL PATH (slowest shard): "
          f"p50={statistics.median(warm):.0f}ms  p99={_percentile(warm, 99):.0f}ms  max={max(warm):.0f}ms")
    if reader_warm:
        print(f"reader (route+reduce, single-thread):  "
              f"p50={statistics.median(reader_warm):.0f}ms  p99={_percentile(reader_warm, 99):.0f}ms  max={max(reader_warm):.0f}ms")
    print(f"=> the full {n_symbols}-ticker vector lands each minute in ~{_percentile(warm, 99):.0f}ms p99 "
          f"(budget 60000ms/minute)")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.bench_stream <n_symbols> <n_shards> <measure_minutes> [warmup] [window]")
    n_symbols, n_shards, measure = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    warmup = int(sys.argv[4]) if len(sys.argv) > 4 else 60
    window = int(sys.argv[5]) if len(sys.argv) > 5 else 245
    total_minutes = warmup + measure

    symbols = synth_symbols(n_symbols)
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}
    root = os.environ.get("BENCH_ROOT", "/tmp/bench_store")

    os.environ["FP_BENCH_LOG"] = "1"
    os.environ["STREAM_URL_OVERRIDE"] = f"ws://127.0.0.1:{PORT}"
    os.environ.setdefault("ALPACA_KEY_ID", "mock")
    os.environ.setdefault("ALPACA_SECRET_KEY", "mock")
    os.environ["MOCK_MINUTES"] = str(total_minutes + 2)

    print(f"streaming {n_symbols} symbols x {total_minutes} minutes through REAL StockDataStream -> mock "
          f"(warmup {warmup}, window {window}); root={root}", flush=True)
    _start_mock(total_minutes + 2)
    time.sleep(1.5)  # let the mock process bind its port before the client connects
    run_sharded_capture(symbols, root, "mock", n_shards=n_shards, window=window,
                        day=SESSION_DAY, max_minutes=total_minutes, snapshots=snapshots)
    _report(root, n_symbols, n_shards, warmup)


if __name__ == "__main__":
    main()
