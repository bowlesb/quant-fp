"""Per-group compute-latency profiler — first-class timing so every feature's cost is visible.

"Time the hell out of every feature." Each FeatureGroup is the vectorized compute unit (one pass
emits all its features), so the natural timing granularity is per group, with per-feature cost
derived. This surfaces a latency table sorted by cost plus a projection to a target ticker scale, so
a newly-added group that is slow is caught immediately — the standing rule is that a feature earns
its place only if it is timed and fast. Backs both a CLI and (later) a latency API endpoint.

Usage: python -m quantlib.features.profile [n_tickers] [window_min] [daily_days] [reps]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.compare import runnable
from quantlib.features.engine import run_group

BASE = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)
INTRADAY_COLS = ("open", "close", "high", "low", "volume", "n_trades", "signed_volume",
                 "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size")


def build_frames(n_tickers: int, window_min: int, daily_days: int) -> dict[str, pl.DataFrame]:
    """Synthetic but schema-faithful frames at a target scale (intraday buffer + daily cache +
    reference snapshot), so the profiler exercises every runnable group."""
    symbols = pl.DataFrame({"symbol": [f"S{i}" for i in range(n_tickers)]})
    minutes = pl.DataFrame({"minute": [BASE + timedelta(minutes=j) for j in range(window_min)]})
    intraday = symbols.join(minutes, how="cross").with_columns(
        [(100.0 + (pl.int_range(pl.len()) % 97) * 0.1).alias(c) for c in INTRADAY_COLS]
    )
    days = pl.DataFrame({"date": [BASE + timedelta(days=j) for j in range(daily_days)]})
    daily = symbols.join(days, how="cross").with_columns(
        [pl.col("date").dt.date()]
        + [(100.0 + (pl.int_range(pl.len()) % 250) * 0.2).alias(c) for c in ("open", "high", "low", "close", "vwap")]
        + [(1e6 + (pl.int_range(pl.len()) % 500) * 1e3).alias("volume")]
    )
    reference = symbols.with_columns(
        [pl.lit("Technology").alias("sector"), pl.lit(True).alias("shortable"),
         pl.lit(True).alias("easy_to_borrow"), pl.lit(True).alias("marginable"), pl.lit(False).alias("fractionable")]
    )
    return {"minute_agg": intraday, "daily": daily, "reference": reference}


def time_group(group: FeatureGroup, frames: dict[str, pl.DataFrame], reps: int = 3, latest: bool = False) -> float:
    """Min wall-clock ms over ``reps`` runs (after a warmup) of one group's compute. ``latest=True`` times
    ``compute_latest`` — the LIVE path (what the per-minute budget actually pays) — instead of compute()."""
    ctx = BatchContext(frames=frames)
    call = (lambda: group.compute_latest(ctx)) if latest else (lambda: run_group(group, ctx, validate=False))
    call()  # warmup
    times = []
    for _ in range(reps):
        start = time.perf_counter()
        call()
        times.append(time.perf_counter() - start)
    return min(times) * 1000.0


def profile(frames: dict[str, pl.DataFrame], reps: int = 3, latest: bool = False) -> pl.DataFrame:
    """Latency table for every runnable group, sorted slowest-first. ``latest`` times the live path."""
    rows = []
    for group in runnable(frames):
        ms = time_group(group, frames, reps, latest=latest)
        n_features = len(group.feature_names)
        rows.append(
            {"group": group.name, "type": group.type.value, "n_features": n_features,
             "ms": round(ms, 1), "us_per_feature": round(ms * 1000.0 / n_features, 1)}
        )
    return pl.DataFrame(rows).sort("ms", descending=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    latest = "--latest" in sys.argv  # time compute_latest (live path) instead of compute() (backfill)
    n_tickers = int(args[0]) if len(args) > 0 else 2000
    window_min = int(args[1]) if len(args) > 1 else 120
    daily_days = int(args[2]) if len(args) > 2 else 250
    reps = int(args[3]) if len(args) > 3 else 5
    frames = build_frames(n_tickers, window_min, daily_days)
    table = profile(frames, reps, latest=latest)
    total_ms = table["ms"].sum()
    total_feats = int(table["n_features"].sum())
    pl.Config.set_tbl_rows(100)
    path = "LIVE (compute_latest)" if latest else "BACKFILL (compute)"
    print(f"=== {path} per-group latency @ {n_tickers} tickers x {window_min}m buffer ({reps} reps, min) ===")
    print(table)
    print(f"\nTOTAL: {total_feats} features across {table.height} groups in {total_ms:.0f} ms "
          f"({1000.0 * total_ms / total_feats:.1f} us/feature) at {n_tickers} tickers")
    print(f"slowest group: {table.row(0, named=True)['group']} ({table.row(0, named=True)['ms']} ms)")


if __name__ == "__main__":
    main()
