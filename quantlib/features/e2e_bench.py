"""End-to-end per-minute latency harness — p50/p99/max, NOT min-of-3.

The audit's honesty point: a min over a few warm reps is the most optimistic number and not the budget.
This runs the FULL live per-minute path — every runnable group's ``compute_latest`` (the aggregate-at-T
form) assembled into one wide frame, exactly as the capture core does — over many iterations and reports
the latency DISTRIBUTION (p50, p99, max), which is what must fit inside the minute alongside inference +
execution. Synthetic dense buffer = an upper bound; on real gappy data the dirty-set computes far fewer
symbols. The production p99 is measured against the live stream (this is the offline proxy + the harness
the production timing reuses).

Usage: python -m quantlib.features.e2e_bench [n_tickers] [window_min] [iters]
"""
from __future__ import annotations

import sys
import time

import polars as pl

from quantlib.features.base import KEY_COLUMNS, BatchContext
from quantlib.features.compare import runnable
from quantlib.features.profile import build_frames


def latest_vector_all(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """The full per-minute vector: every runnable group's compute_latest, outer-joined into one wide
    frame keyed by (symbol, minute) — the assembly the live capture does each minute."""
    ctx = BatchContext(frames=frames)
    out: pl.DataFrame | None = None
    for group in runnable(frames):
        piece = group.compute_latest(ctx)
        out = piece if out is None else out.join(piece, on=list(KEY_COLUMNS), how="full", coalesce=True)
    return out if out is not None else pl.DataFrame()


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    n_tickers = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    window_min = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    iters = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    frames = build_frames(n_tickers, window_min, 250)
    vec = latest_vector_all(frames)  # warmup
    n_features = vec.width - 2
    times = []
    for _ in range(iters):
        start = time.perf_counter()
        latest_vector_all(frames)
        times.append((time.perf_counter() - start) * 1000.0)
    print(f"END-TO-END latest-minute vector: {n_features} feats x {n_tickers} tickers x {window_min}m buffer, {iters} iters")
    print(f"  p50 {_pct(times, 0.50):.0f} ms | p99 {_pct(times, 0.99):.0f} ms | max {max(times):.0f} ms | min {min(times):.0f} ms")
    print("  (dense synthetic = upper bound; real gappy data computes only the dirty-set; sharding /N on top)")


if __name__ == "__main__":
    main()
