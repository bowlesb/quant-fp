"""Honest memory + compute bench for Monday scale (10k tickers × features × HORIZONS).

The live minute boundary does NOT hold months of minute data. It holds two small resident caches:
  - an INTRADAY minute buffer (recent ~window minutes) for intraday features, and
  - a DAILY-history cache (one row per symbol-day, ~months) for multi-day features.
This allocates both at the target scale, plus the output vector, computes a minute, and reports peak
RSS — the real Monday number, not a single-day toy on replicated symbols.

Usage: python -m quantlib.features.mem_bench [n_tickers] [window_min] [daily_days] [n_feature_cols]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_all

BASE = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)
INTRADAY_COLS = ["close", "high", "low", "n_trades", "signed_volume", "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size"]


def _rss_mb() -> float:
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024.0
    return -1.0


def _grid(n_tickers: int, n_rows: int, time_col: str, base, step) -> pl.DataFrame:
    symbols = pl.DataFrame({"symbol": [f"S{i}" for i in range(n_tickers)]})
    times = pl.DataFrame({time_col: [base + step * j for j in range(n_rows)]})
    return symbols.join(times, how="cross")


def main() -> None:
    n_tickers = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    window_min = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    daily_days = int(sys.argv[3]) if len(sys.argv) > 3 else 250
    n_feature_cols = int(sys.argv[4]) if len(sys.argv) > 4 else 1000
    print(f"baseline RSS: {_rss_mb():.0f} MB")

    intraday = _grid(n_tickers, window_min, "minute", BASE, timedelta(minutes=1)).with_columns(
        [(100.0 + (pl.int_range(pl.len()) % 97) * 0.1).alias(c) for c in INTRADAY_COLS]
    )
    print(f"intraday buffer  {n_tickers}x{window_min} ({intraday.height:,} rows): RSS {_rss_mb():.0f} MB")

    daily = _grid(n_tickers, daily_days, "date", BASE, timedelta(days=1)).with_columns(
        [(100.0 + (pl.int_range(pl.len()) % 250) * 0.2).alias(f"d{c}") for c in range(10)]
    )
    print(f"+ daily cache    {n_tickers}x{daily_days} ({daily.height:,} rows): RSS {_rss_mb():.0f} MB")

    vector = _grid(n_tickers, 1, "minute", BASE, timedelta(minutes=1)).with_columns(
        [(pl.int_range(pl.len()) % 13 * 0.07).alias(f"f{c}") for c in range(n_feature_cols)]
    )
    print(f"+ {n_feature_cols}-feature vector ({vector.height:,} rows): RSS {_rss_mb():.0f} MB")

    groups = runnable({"minute_agg": intraday})
    n_features = sum(len(g.feature_names) for g in groups)
    run_all(groups, BatchContext(frames={"minute_agg": intraday}), validate=False)  # warmup
    times = []
    for _ in range(3):
        start = time.perf_counter()
        run_all(groups, BatchContext(frames={"minute_agg": intraday}), validate=False)
        times.append(time.perf_counter() - start)
    print(f"compute {n_features} live feats over the buffer: {min(times)*1000:.0f} ms | PEAK RSS {_rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
