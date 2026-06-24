"""``ValueInputRing`` parity: the carried value-input ring reconstructs the per-minute ``_matrix_at`` slice
(``frame.filter(<=T).sort.group_by.tail(max_lag+1)``) so that ``_derived_row`` over the reconstruction is
byte-identical to ``_derived_row`` over the buffer-tail — on dense AND sparse symbols. This is the engine-core
analogue of test_fp_point_ring: same positional-row-ring primitive, at depth max_lag+1 over the raw input
columns instead of depth 121 over point sources.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.point_ring import ValueInputRing
from quantlib.features.profile import build_frames, runs_incremental

BASE = dt.datetime(2026, 6, 16, 13, 30, tzinfo=dt.timezone.utc)


def _engine_and_buffer(sparse: bool):
    frames = build_frames(40, 130, 250, include_trades=True)
    buf = frames["minute_agg"]
    if sparse:
        symbols = sorted(buf["symbol"].unique().to_list())
        gap = set(symbols[: len(symbols) // 2])
        minutes = sorted(buf["minute"].unique())
        idx = {m: i for i, m in enumerate(minutes)}
        buf = buf.filter(
            ~pl.struct(["symbol", "minute"]).map_elements(
                lambda r: idx[r["minute"]] > 0 and idx[r["minute"]] % 7 == 0 and r["symbol"] in gap,
                return_dtype=pl.Boolean,
            )
        )
        frames["minute_agg"] = buf
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    engine = IncrementalEngine(groups)
    engine.seed(buf)
    return engine, buf


def _buffer_tail_derive(engine: IncrementalEngine, buf: pl.DataFrame, latest: object) -> pl.DataFrame:
    source = (
        buf.filter(pl.col("minute") <= latest)
        .sort("minute")
        .group_by("symbol", maintain_order=True)
        .tail(engine.max_lag + 1)
    )
    row = (
        engine._derived_row_rust(source, latest)
        if engine.rust_slice
        else engine._derived_row(source, latest)
    )
    return row.sort("symbol")


def _ring_derive(engine: IncrementalEngine, buf: pl.DataFrame, latest: object) -> pl.DataFrame:
    value_cols = [c for c in engine.input_cols if c not in ("symbol", "minute")]
    ring = ValueInputRing(engine.symbols or [], value_cols, engine.max_lag + 1)
    for minute in sorted(buf["minute"].unique()):
        ring.fold(buf.filter(pl.col("minute") == minute).select(["symbol", "minute", *value_cols]))
    source = ring.materialize_tail(buf.schema["minute"])
    row = (
        engine._derived_row_rust(source, latest)
        if engine.rust_slice
        else engine._derived_row(source, latest)
    )
    return row.sort("symbol")


@pytest.mark.parametrize("sparse", [False, True])
def test_value_input_ring_derive_matches_buffer_tail(sparse: bool) -> None:
    engine, buf = _engine_and_buffer(sparse)
    latest = buf["minute"].max()
    truth = _buffer_tail_derive(engine, buf, latest)
    ring = _ring_derive(engine, buf, latest)
    assert truth["symbol"].to_list() == ring["symbol"].to_list()
    ring = ring.select(truth.columns)
    for col in truth.columns:
        if truth[col].dtype == pl.Float64:
            import numpy as np

            a = truth[col].to_numpy()
            b = ring[col].to_numpy()
            both_nan = np.isnan(a) & np.isnan(b)
            assert (both_nan | (np.abs(a - b) <= 1e-12)).all(), f"{col} differs (sparse={sparse})"
        else:
            assert truth[col].equals(ring[col]), f"{col} differs (sparse={sparse})"


def _step_sequence(sparse: bool) -> dict[str, pl.DataFrame]:
    frames = build_frames(40, 130, 250, include_trades=True)
    buf = frames["minute_agg"]
    if sparse:
        symbols = sorted(buf["symbol"].unique().to_list())
        gap = set(symbols[: len(symbols) // 2])
        minutes = sorted(buf["minute"].unique())
        idx = {m: i for i, m in enumerate(minutes)}
        buf = buf.filter(
            ~pl.struct(["symbol", "minute"]).map_elements(
                lambda r: idx[r["minute"]] > 0 and idx[r["minute"]] % 7 == 0 and r["symbol"] in gap,
                return_dtype=pl.Boolean,
            )
        )
        frames["minute_agg"] = buf
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    minutes = sorted(buf["minute"].unique())
    engine = IncrementalEngine(groups)
    out: dict[str, pl.DataFrame] = {}
    for minute in minutes[-3:]:
        out = engine.step(buf.filter(pl.col("minute") <= minute))
    return out


@pytest.mark.parametrize("rust_reduce", ["0", "1"])
@pytest.mark.parametrize("sparse", [False, True])
def test_fp_matrix_ring_engine_byte_identical(
    monkeypatch: pytest.MonkeyPatch, sparse: bool, rust_reduce: str
) -> None:
    """The gate: ``IncrementalEngine.step`` with FP_MATRIX_RING on == off, on the live streaming sequence, at
    FR=0 AND FR=1 (the FP_RUST_REDUCE rebase_time_axis straddle case — price_volume's obv_slope time-OLS
    co-resides, the one interaction a standalone test could miss), dense AND sparse."""
    import numpy as np

    monkeypatch.setenv("FP_RUST_REDUCE", rust_reduce)
    monkeypatch.setenv("FP_MATRIX_RING", "0")
    off = _step_sequence(sparse)
    monkeypatch.setenv("FP_MATRIX_RING", "1")
    on = _step_sequence(sparse)
    assert set(off) == set(on)
    for name in off:
        a = off[name].sort("symbol")
        b = on[name].sort("symbol").select(a.columns)
        for col in a.columns:
            if a[col].dtype == pl.Float64:
                x = a[col].to_numpy()
                y = b[col].to_numpy()
                both_nan = np.isnan(x) & np.isnan(y)
                assert (
                    both_nan | (np.abs(x - y) <= 1e-12)
                ).all(), f"{name}.{col}: FP_MATRIX_RING on/off differ (sparse={sparse}, FR={rust_reduce})"
            else:
                assert a[col].equals(b[col]), f"{name}.{col} differ (sparse={sparse}, FR={rust_reduce})"
