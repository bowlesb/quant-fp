"""FP_EMIT_NUMPY_PARITY shadow: the monitoring-only live self-check comparing the served ``emit_numpy``
assemble to the polars ``assemble_from_long`` truth, with a NULL-MASK-AWARE verdict. The load-bearing case is
the null-vs-NaN flip — a value-only (NaN-tolerant) compare would MISS it, which is exactly the bug the
``fill_nan(None)`` fix addresses. These tests pin: clean = no breach; a value perturbation = breach; a
NULL→finite flip (the null-mask divergence) = breach; and the served output is unchanged (monitoring-only)."""
from __future__ import annotations

import polars as pl
import pytest

from quantlib.features import metrics
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, assemble_from_long
from quantlib.features.incremental import IncrementalEngine, _record_emit_numpy_parity
from quantlib.features.profile import build_frames, runs_incremental


def _breach_count() -> float:
    return metrics.EMIT_NUMPY_BREACH._value.get()


def _served_and_truth() -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
    frames = build_frames(24, 130, 250, include_trades=True)
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    buf = frames["minute_agg"]
    engine = IncrementalEngine(groups)
    engine.seed(buf)
    engine._fold_latest(buf, buf["minute"].max(), slice_derive=True)
    latest = buf["minute"].max()
    latest_frame = engine._latest_frame(buf, latest)
    truth = assemble_from_long(
        groups, engine._running_long(), latest_frame, latest, engine.plan, engine.reg_plan, engine.centered
    )
    # the served path is emit_numpy; reuse the truth as a stand-in served-clean (they're byte-identical) and
    # perturb copies for the breach cases.
    served = {name: frame.clone() for name, frame in truth.items()}
    return served, truth


def test_clean_emit_records_no_breach() -> None:
    served, truth = _served_and_truth()
    before = _breach_count()
    _record_emit_numpy_parity(served, truth)
    assert _breach_count() == before, "byte-identical emit must NOT record a breach"
    assert metrics.EMIT_NUMPY_MAX_ABS_DIFF._value.get() <= 1e-12


def test_value_perturbation_records_breach() -> None:
    served, truth = _served_and_truth()
    name = next(iter(served))
    float_col = next(c for c in served[name].columns if served[name].schema[c] == pl.Float64)
    served[name] = served[name].with_columns((pl.col(float_col) + 1e-6).alias(float_col))
    before = _breach_count()
    _record_emit_numpy_parity(served, truth)
    assert _breach_count() == before + 1, "a >1e-12 value divergence must record a breach"


def test_null_to_finite_flip_records_breach() -> None:
    """The load-bearing case: a cell that is NULL in the truth but FINITE in the served output (the null→NaN
    representational divergence). A value-only NaN-tolerant compare would MISS this; the null-mask check catches
    it. Find a group/column with at least one null in the truth, set the served cell to a finite value."""
    served, truth = _served_and_truth()
    target = None
    for name, frame in truth.items():
        for col in frame.columns:
            if frame.schema[col] == pl.Float64 and frame[col].is_null().any():
                target = (name, col)
                break
        if target:
            break
    if target is None:
        pytest.skip("no null feature cell in the truth to exercise the null-mask flip")
    name, col = target
    # set the served column's nulls to a finite value -> null mask now differs from truth
    served[name] = served[name].with_columns(pl.col(col).fill_null(0.0).alias(col))
    before = _breach_count()
    _record_emit_numpy_parity(served, truth)
    assert _breach_count() == before + 1, "a null→finite flip must record a breach (the null-mask check)"


def test_shadow_never_alters_served_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """MONITORING-ONLY: the engine's served step output is byte-identical whether FP_EMIT_NUMPY_PARITY is on or
    off (with FP_EMIT_NUMPY armed) — the shadow records a metric, never changes values."""
    monkeypatch.setenv("FP_EMIT_NUMPY", "1")
    frames = build_frames(24, 130, 250, include_trades=True)
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    buf = frames["minute_agg"]
    minutes = sorted(buf["minute"].unique())

    def served(parity: str) -> dict[str, pl.DataFrame]:
        monkeypatch.setenv("FP_EMIT_NUMPY_PARITY", parity)
        engine = IncrementalEngine(groups)
        out: dict[str, pl.DataFrame] = {}
        for minute in minutes[-3:]:
            out = engine.step(buf.filter(pl.col("minute") <= minute))
        return out

    off = served("0")
    on = served("1")
    for name in off:
        a = off[name].sort("symbol")
        b = on[name].sort("symbol").select(a.columns)
        assert a.equals(b), f"{name}: FP_EMIT_NUMPY_PARITY altered the served output"
