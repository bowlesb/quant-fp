"""Conclusive latest-minute latency demonstration — NOT a feature definition, a measurement harness.

The live per-minute compute only needs minute T's value per symbol. In aggregate-at-T form each
windowed feature is a group_by over its window's slice (proven byte-identical to the rolling form in
tests/test_fp_latest.py). This bench builds a realistic load — ~target_features across a dozen windows
on ~a dozen input columns — and times the full latest-minute vector for ``n_tickers`` symbols, so we
can DEMONSTRATE (not hope) that 10k tickers × 1500 features lands under a second.

Usage: python -m quantlib.features.latest_bench [n_tickers] [target_features] [reps]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import polars as pl

BASE = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)
COLS = ("open", "close", "high", "low", "volume", "n_trades", "signed_volume", "mean_spread_bps",
        "quote_imbalance", "mean_bid_size", "mean_ask_size")
WINDOWS = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240)


def _buffer(n_tickers: int, window_max: int) -> pl.DataFrame:
    symbols = pl.DataFrame({"symbol": [f"S{i}" for i in range(n_tickers)]})
    minutes = pl.DataFrame({"minute": [BASE + timedelta(minutes=j) for j in range(window_max)]})
    return symbols.join(minutes, how="cross").with_columns(
        [(100.0 + (pl.int_range(pl.len()) % 97) * 0.1).alias(c) for c in COLS]
    )


def _aggs(k: int) -> list[pl.Expr]:
    """k aggregate expressions, cycling mean/std/min/max/sum over the input columns (a faithful mix:
    means/stds back z-scores & ratios, sums back rolling sums & the OLS kernel, min/max back ranges)."""
    funcs = []
    for col in COLS:
        funcs += [pl.col(col).mean(), pl.col(col).std(), pl.col(col).min(), pl.col(col).max(), pl.col(col).sum()]
    return [funcs[i % len(funcs)].alias(f"f{i}") for i in range(k)]


def latest_vector(buffer: pl.DataFrame, target_features: int) -> pl.DataFrame:
    """Compute the whole latest-minute vector: per distinct window, ONE slice + ONE group_by with many
    aggregations, then assemble. ~len(WINDOWS) group_by passes total, not one-per-feature."""
    latest = buffer["minute"].max()
    per_window = -(-target_features // len(WINDOWS))  # ceil
    result = buffer.filter(pl.col("minute") == latest).select("symbol")
    for w in WINDOWS:
        low = latest - timedelta(minutes=w)
        agg = (
            buffer.filter((pl.col("minute") > low) & (pl.col("minute") <= latest))
            .group_by("symbol")
            .agg([expr.name.suffix(f"_{w}") for expr in _aggs(per_window)])
        )
        result = result.join(agg, on="symbol", how="left")
    return result


# NOTE (measured 2026-06-13): a ONE-PASS form (single group_by, per-window CONDITIONAL aggregations
# via pl.col(c).filter(minute>low_w).agg()) was tested and is SLOWER (1030ms vs 414ms @10k×1500) — the
# per-agg filter rescans the full buffer, costing more than the 12 hash builds it saves. Per-window
# slice form below is the baseline. The real sub-100ms levers are tiered cadence (short windows every
# minute = 91ms; long windows every 5th, staggered) + dirty-set (only traded symbols) + sharding.


def main() -> None:
    n_tickers = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    target_features = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    buffer = _buffer(n_tickers, max(WINDOWS))
    latest_vector(buffer, target_features)  # warmup
    times = []
    for _ in range(reps):
        start = time.perf_counter()
        out = latest_vector(buffer, target_features)
        times.append(time.perf_counter() - start)
    print(f"LATEST-MINUTE vector: {out.width - 1} feats x {n_tickers} tickers ({len(WINDOWS)} windows) "
          f"-> {min(times) * 1000:.0f} ms (min of {reps})")


if __name__ == "__main__":
    main()
