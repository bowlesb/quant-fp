"""Incremental windowed-sum accumulator == the batch Rust recompute, cell-for-cell.

Proves the pre-prepped-state design is parity-safe: folding minutes in one at a time (add new, expire old)
yields the SAME per-(symbol, window) sums as re-scanning the whole buffer with quant_tick.windowed_sums.
That's the foundation for moving the per-minute work off the critical path without breaking parity.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.incremental import WindowedSumState
from quantlib.features.latest import rust_windowed_sums

START = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def test_incremental_matches_rust_windowed_sums() -> None:
    n_sym, n_min, n_cols = 60, 90, 3
    windows = (5, 10, 20, 45)
    symbols = [f"S{i:03d}" for i in range(n_sym)]
    rng = np.random.default_rng(0)

    minute_mats = [rng.standard_normal((n_sym, n_cols)) for _ in range(n_min)]  # per-minute value matrix
    minutes = [START + dt.timedelta(minutes=i) for i in range(n_min)]
    epochs = [int(m.timestamp()) for m in minutes]

    # the same data as a long frame for the batch recompute
    frame = pl.DataFrame(
        {
            "symbol": np.repeat(symbols, n_min),
            "minute": minutes * n_sym,
            "v0": [minute_mats[m][s, 0] for s in range(n_sym) for m in range(n_min)],
            "v1": [minute_mats[m][s, 1] for s in range(n_sym) for m in range(n_min)],
            "v2": [minute_mats[m][s, 2] for s in range(n_sym) for m in range(n_min)],
        }
    ).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))).sort(["symbol", "minute"])
    batch = rust_windowed_sums(frame, ["v0", "v1", "v2"], windows)

    state = WindowedSumState(symbols, windows, n_cols)
    for epoch, mat in zip(epochs, minute_mats):
        state.update(epoch, mat)
        state.trim()

    for w in windows:
        inc = state.sums(w)  # (n_sym, n_cols), running sum at the final minute
        sub = batch.filter(pl.col("window") == w)
        ref = {s: (a, b, c) for s, a, b, c in zip(sub["symbol"], sub["v0"], sub["v1"], sub["v2"])}
        for si, sym in enumerate(symbols):
            for ci in range(n_cols):
                assert abs(inc[si, ci] - ref[sym][ci]) <= 1e-9, f"window {w} {sym} col{ci}: {inc[si, ci]} vs {ref[sym][ci]}"
