"""Cell-for-cell parity gate for the Rust stateful kernels (quant_tick.rolling_extrema /
time_lag_gather) against the already-parity-gated Python folds (ExtremaState / LastKState).

The Rust kernels move the per-symbol extrema-deque / lag-ring fold off the Python critical path. They
compute the SAME trailing ``(T−w, T]`` extrema and the SAME time-based lag fresh from the buffer each
minute, so they must equal the Python fold cell-for-cell — INCLUDING warmup (an empty window / first-bar
holes) AND a GAPPY grid (a symbol missing a prior minute, so a time-based lag lands on an absent minute and
must be null). The Python fold emits NaN for a missing cell; the Rust gather restores it to Polars null
(matching the backfill ``rolling_*_by`` / self-join) — so equality is checked with NaN(python)==null(rust)
treated as the same MISSING, and every finite cell required equal to tol 0.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.stateful import (
    ExtremaSpec,
    ExtremaState,
    LagSpec,
    LastKState,
    rust_extrema,
    rust_lags,
)

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 6, n_min: int = 70, seed: int = 19, drop: set[tuple[int, int]] | None = None) -> pl.DataFrame:
    """OHLC minute stream. ``drop`` = a set of (symbol_index, minute_index) bars to OMIT — making the grid
    GAPPY so a fixed-offset time lag can land on an absent minute (the null-hole case)."""
    drop = drop or set()
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.003)
            if (s, mi) in drop:
                continue
            close = price[s]
            opn = close * (1.0 + rng.standard_normal() * 0.001)
            high = max(opn, close) * (1.0 + abs(rng.standard_normal()) * 0.001)
            low = min(opn, close) * (1.0 - abs(rng.standard_normal()) * 0.001)
            rows.append({"symbol": f"S{s}", "minute": minute, "open": opn, "high": high, "low": low, "close": close})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _python_fold_extrema(stream: pl.DataFrame, specs: list[ExtremaSpec], latest: dt.datetime) -> dict[str, np.ndarray]:
    symbols = sorted(stream["symbol"].unique().to_list())
    state = ExtremaState(symbols, specs)
    sym_index = {symbol: i for i, symbol in enumerate(symbols)}
    sources = sorted({spec.source for spec in specs})
    for minute in sorted(stream["minute"].unique()):
        present = stream.filter(pl.col("minute") == minute)
        cols: dict[str, np.ndarray] = {}
        for source in sources:
            arr = np.full(len(symbols), np.nan, dtype=np.float64)
            for symbol, value in zip(present["symbol"].to_list(), present[source].to_list()):
                arr[sym_index[symbol]] = value
            cols[source] = arr
        state.fold(int(minute.timestamp()), cols)
    return {spec.alias: state.extremum(spec.alias) for spec in specs}


def _python_fold_lags(stream: pl.DataFrame, specs: list[LagSpec], latest: dt.datetime) -> dict[str, np.ndarray]:
    symbols = sorted(stream["symbol"].unique().to_list())
    state = LastKState(symbols, specs)
    sym_index = {symbol: i for i, symbol in enumerate(symbols)}
    sources = sorted({spec.source for spec in specs})
    for minute in sorted(stream["minute"].unique()):
        present = stream.filter(pl.col("minute") == minute)
        cols: dict[str, np.ndarray] = {}
        for source in sources:
            arr = np.full(len(symbols), np.nan, dtype=np.float64)
            for symbol, value in zip(present["symbol"].to_list(), present[source].to_list()):
                arr[sym_index[symbol]] = value
            cols[source] = arr
        state.fold(int(minute.timestamp()), cols)
    return {spec.alias: state.lag(spec.alias) for spec in specs}


def _assert_cellwise(python: dict[str, np.ndarray], rust: pl.DataFrame, symbols: list[str], label: str) -> None:
    """Every cell equal: a MISSING cell is NaN in the python fold and null in the rust gather (same missing);
    every FINITE cell must match to tol 0 (bit-for-bit, since both read the same source values)."""
    rust = rust.sort("symbol")
    assert rust["symbol"].to_list() == symbols, f"{label}: symbol order differs"
    for alias, py_arr in python.items():
        ru_arr = rust[alias].to_numpy()
        ru_null = rust[alias].is_null().to_numpy()
        for i in range(len(symbols)):
            py_missing = not np.isfinite(py_arr[i])
            ru_missing = bool(ru_null[i]) or not np.isfinite(ru_arr[i])
            assert py_missing == ru_missing, f"{label}.{alias}[{symbols[i]}]: missing mismatch (py={py_arr[i]} ru_null={ru_null[i]})"
            if not py_missing:
                assert py_arr[i] == ru_arr[i], f"{label}.{alias}[{symbols[i]}]: {py_arr[i]} != {ru_arr[i]} (tol 0)"


def test_rust_extrema_equals_python_fold_dense() -> None:
    stream = _stream(n_sym=6, n_min=70)
    windows = (5, 10, 15, 30, 60, 120, 240)
    specs = [ExtremaSpec(alias=f"_hi_{w}", source="high", window=w, op="max") for w in windows]
    specs += [ExtremaSpec(alias=f"_lo_{w}", source="low", window=w, op="min") for w in windows]
    symbols = sorted(stream["symbol"].unique().to_list())
    # Check at EVERY minute incl. warmup (small windows warm immediately, 240m never fully in 70 minutes).
    for latest in sorted(stream["minute"].unique()):
        buffer = stream.filter(pl.col("minute") <= latest)
        python = _python_fold_extrema(buffer, specs, latest)
        rust = rust_extrema(buffer, specs, latest)
        _assert_cellwise(python, rust, symbols, f"extrema@{latest}")


def test_rust_lags_equals_python_fold_dense() -> None:
    stream = _stream(n_sym=6, n_min=70)
    windows = (1, 2, 3, 5, 10, 30, 60, 90, 120, 180)
    specs = [LagSpec(alias=f"_lag{w}", source="close", minutes=w) for w in windows]
    symbols = sorted(stream["symbol"].unique().to_list())
    for latest in sorted(stream["minute"].unique()):
        buffer = stream.filter(pl.col("minute") <= latest)
        python = _python_fold_lags(buffer, specs, latest)
        rust = rust_lags(buffer, specs, latest)
        _assert_cellwise(python, rust, symbols, f"lags@{latest}")


def test_rust_extrema_equals_python_fold_gappy() -> None:
    """A GAPPY grid: holes in the middle of each symbol's history. The trailing extrema must ignore the
    absent minutes identically (present-bar semantics) in both the Rust kernel and the Python deque."""
    drop = {(s, mi) for s in range(6) for mi in range(20, 40) if (s + mi) % 3 == 0}
    stream = _stream(n_sym=6, n_min=70, drop=drop)
    windows = (5, 15, 30, 60)
    specs = [ExtremaSpec(alias=f"_hi_{w}", source="high", window=w, op="max") for w in windows]
    specs += [ExtremaSpec(alias=f"_lo_{w}", source="low", window=w, op="min") for w in windows]
    symbols = sorted(stream["symbol"].unique().to_list())
    for latest in sorted(stream["minute"].unique()):
        buffer = stream.filter(pl.col("minute") <= latest)
        python = _python_fold_extrema(buffer, specs, latest)
        rust = rust_extrema(buffer, specs, latest)
        _assert_cellwise(python, rust, symbols, f"extrema-gappy@{latest}")


def test_rust_lags_equals_python_fold_gappy() -> None:
    """A GAPPY grid: a time-based lag landing on an absent minute is null in BOTH (the self-join contract),
    and a lag landing on a present minute reads that exact bar in BOTH — cell-for-cell across the holes."""
    drop = {(s, mi) for s in range(6) for mi in range(20, 50) if (s * 2 + mi) % 4 == 0}
    stream = _stream(n_sym=6, n_min=70, drop=drop)
    windows = (1, 2, 3, 5, 10, 15, 30, 60)
    specs = [LagSpec(alias=f"_lag{w}", source="close", minutes=w) for w in windows]
    symbols = sorted(stream["symbol"].unique().to_list())
    for latest in sorted(stream["minute"].unique()):
        buffer = stream.filter(pl.col("minute") <= latest)
        python = _python_fold_lags(buffer, specs, latest)
        rust = rust_lags(buffer, specs, latest)
        _assert_cellwise(python, rust, symbols, f"lags-gappy@{latest}")
