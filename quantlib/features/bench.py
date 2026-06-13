"""Latency bench — measure feature-compute time at scale before Monday (FP2 / FP_GOALS D).

Replicates a real day's minute aggregates up to a target ticker count and times the full feature
compute over a trailing window (the per-minute-boundary cost). Finds where we are vs the ≤2 s budget.

Usage: python -m quantlib.features.bench <YYYY-MM-DD> <target_tickers> [window_minutes]
"""
from __future__ import annotations

import sys
import time

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_all
from quantlib.features.loaders import load_minute_agg


def _replicate(frame: pl.DataFrame, factor: int) -> pl.DataFrame:
    parts = [frame]
    for i in range(1, factor):
        parts.append(frame.with_columns((pl.col("symbol") + f"_r{i}").alias("symbol")))
    return pl.concat(parts)


def main() -> None:
    day = sys.argv[1]
    target = int(sys.argv[2])
    window = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    base = load_minute_agg(day, "backfill")
    base_n = base["symbol"].n_unique()
    big = _replicate(base, max(1, -(-target // base_n)))  # ceil division
    actual_n = big["symbol"].n_unique()

    recent = sorted(big["minute"].unique())[-window:]
    buffer = big.filter(pl.col("minute").is_in(recent))
    frames = {"minute_agg": buffer}
    groups = runnable(frames)
    n_features = sum(len(g.feature_names) for g in groups)

    run_all(groups, BatchContext(frames=frames), validate=False)  # warmup (JIT/alloc)
    times = []
    for _ in range(5):
        start = time.perf_counter()
        run_all(groups, BatchContext(frames=frames), validate=False)
        times.append(time.perf_counter() - start)
    times.sort()
    p50, mx = times[len(times) // 2], times[-1]
    rows = buffer.height
    print(
        f"tickers={actual_n}  features={n_features}  window={window}m  rows={rows:,}  "
        f"compute p50={p50 * 1000:.0f}ms  max={mx * 1000:.0f}ms  "
        f"({'OK <=2s' if mx <= 2.0 else 'OVER 2s budget'})"
    )


if __name__ == "__main__":
    main()
