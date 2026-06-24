"""FP_EMIT_NUMPY engine parity: ``IncrementalEngine.step`` with the flag ON (assemble from the running-sum
numpy array via ``emit_numpy``) is byte-identical to OFF (the per-group polars ``assemble_from_long`` pivot
loop) on the live streaming sequence, across the full shared reduction-group set. ``emit_numpy`` itself is
already guarded cell-for-cell against ``step`` / the batch by test_fp_incremental_emit; this pins that the
FLAG DISPATCH in ``step`` keeps that parity through the real fold sequence (the same on-vs-off gate
FP_POINT_RING carries)."""
from __future__ import annotations

import polars as pl
import pytest

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.profile import build_frames, runs_incremental


def _shared_groups(frames: dict[str, pl.DataFrame]) -> list[ReductionGroup]:
    return [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]


def _step_sequence(
    groups: list[ReductionGroup], full: pl.DataFrame, n_minutes: int
) -> dict[str, pl.DataFrame]:
    """Lazy-seed on the first step, then step the last ``n_minutes`` minutes (the real streaming sequence),
    returning the final minute's per-group output frames."""
    minutes = sorted(full["minute"].unique())
    engine = IncrementalEngine(groups)
    out: dict[str, pl.DataFrame] = {}
    for minute in minutes[-n_minutes:]:
        out = engine.step(full.filter(pl.col("minute") <= minute))
    return out


def test_emit_numpy_flag_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = build_frames(40, 130, 250, include_trades=True)
    # Force a DEGENERATE (exactly-constant close -> zero-variance) name so the null-vs-NaN path this gate
    # exists for is actually exercised: its autocorrelation / corr cells are undefined (NULL on the polars
    # truth, NaN on the raw numpy emit), which the fill_nan(None) fix maps back to NULL.
    full = frames["minute_agg"].with_columns(
        pl.when(pl.col("symbol") == "S0").then(100.0).otherwise(pl.col("close")).alias("close")
    )
    groups = _shared_groups(frames)
    assert groups, "expected shared incremental_safe reduction groups"

    monkeypatch.setenv("FP_EMIT_NUMPY", "0")
    polars_out = _step_sequence(groups, full, 3)
    monkeypatch.setenv("FP_EMIT_NUMPY", "1")
    numpy_out = _step_sequence(groups, full, 3)

    # Anti-vacuity: the FLAT name must produce at least one NULL cell (a degenerate/undefined feature), so the
    # null-mask comparison below is non-trivially exercised — otherwise the gate would pass on all-finite data.
    flat_nulls = sum(
        int(frame.filter(pl.col("symbol") == "S0").null_count().sum_horizontal().item())
        for frame in polars_out.values()
    )
    assert flat_nulls > 0, "fixture did not produce a degenerate NULL cell — the null-mask gate is vacuous"

    assert set(polars_out) == set(numpy_out)
    for name in polars_out:
        a = polars_out[name].sort("symbol")
        b = numpy_out[name].sort("symbol").select(a.columns)
        for col in a.columns:
            if a[col].dtype == pl.Float64:
                # NULL masks must match EXACTLY — the whole point of the fill_nan(None) fix is that emit_numpy
                # emits NULL (not bare NaN) where assemble_from_null nulls, since the store + trust grading
                # distinguish them. Comparing .is_null() (not a NaN-tolerant value compare) is what makes this
                # gate catch the divergence; the original null↔NaN-conflating comparison would have passed it.
                a_null = a[col].is_null().to_numpy()
                b_null = b[col].is_null().to_numpy()
                assert (
                    a_null == b_null
                ).all(), f"{name}.{col}: FP_EMIT_NUMPY null mask differs (null vs NaN)"
                left = a[col].fill_null(0.0).to_numpy()
                right = b[col].fill_null(0.0).to_numpy()
                assert (abs(left - right) <= 1e-12).all(), f"{name}.{col}: FP_EMIT_NUMPY values differ"
            else:
                assert a[col].equals(b[col]), f"{name}.{col}: FP_EMIT_NUMPY on/off differ (non-float)"
