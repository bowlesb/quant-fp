"""FP_POINT_RING_PARITY shadow: the monitoring-only live self-check that compares the PointRing's carried
``__pt_`` columns to the whole-buffer ``resolve_points`` truth and records a breach metric — never altering
the served value. Mirrors FP_INCREMENTAL_PARITY. These tests pin: a clean ring records no breach; a perturbed
cell / a dropped symbol records a breach; and the served output is the ring's regardless (monitoring-only)."""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import metrics
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, resolve_points
from quantlib.features.incremental import _record_point_ring_parity
from quantlib.features.point_ring import PointRing, point_frame_from_ring, point_specs
from quantlib.features.profile import build_frames, runs_incremental

BASE = dt.datetime(2026, 6, 16, 13, 30, tzinfo=dt.timezone.utc)


def _breach_count() -> float:
    return metrics.POINT_RING_BREACH._value.get()


def _ring_and_truth() -> tuple[pl.DataFrame, pl.DataFrame]:
    frames = build_frames(24, 130, 250, include_trades=True)
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    buf = frames["minute_agg"]
    latest = buf["minute"].max()
    symbols = sorted(buf["symbol"].unique().to_list())
    ring = PointRing(symbols, point_specs(groups))
    for minute in sorted(buf["minute"].unique()):
        ring.fold(buf.filter(pl.col("minute") == minute))
    ring_frame = point_frame_from_ring(groups, ring, symbols, latest)
    truth = resolve_points(groups, buf, latest)
    return ring_frame, truth


def test_clean_ring_records_no_breach() -> None:
    """The ring == resolve_points (it always does on real fixtures) -> no breach, max-abs-diff ~0."""
    ring_frame, truth = _ring_and_truth()
    before = _breach_count()
    _record_point_ring_parity(ring_frame, truth)
    assert _breach_count() == before, "a byte-identical ring must NOT record a breach"
    assert metrics.POINT_RING_MAX_ABS_DIFF._value.get() <= 1e-12


def test_perturbed_cell_records_breach() -> None:
    """A single __pt_ cell beyond 1e-12 absolute -> breach, and the gauge reflects the magnitude."""
    ring_frame, truth = _ring_and_truth()
    point_col = next(c for c in ring_frame.columns if c.startswith("__pt_"))
    # bump one symbol's cell by 1e-6 (>> 1e-12 tol)
    perturbed = ring_frame.with_columns(
        pl.when(pl.col("symbol") == ring_frame["symbol"][0])
        .then(pl.col(point_col) + 1e-6)
        .otherwise(pl.col(point_col))
        .alias(point_col)
    )
    before = _breach_count()
    _record_point_ring_parity(perturbed, truth)
    assert _breach_count() == before + 1, "a >1e-12 cell divergence must record a breach"
    assert metrics.POINT_RING_MAX_ABS_DIFF._value.get() >= 1e-7


def test_dropped_symbol_records_breach() -> None:
    """A symbol present in resolve_points but MISSING from the ring (only_truth>0) -> breach (the ring dropped
    a symbol it should carry)."""
    ring_frame, truth = _ring_and_truth()
    dropped = ring_frame.filter(pl.col("symbol") != ring_frame["symbol"][0])
    before = _breach_count()
    _record_point_ring_parity(dropped, truth)
    assert _breach_count() == before + 1, "a symbol in truth but absent from the ring must record a breach"


def test_extra_ring_symbol_is_not_a_breach() -> None:
    """The ring is a SUPERSET (fixed session index); a symbol in the ring but NOT in resolve_points
    (only_ring) is expected coverage, not a breach."""
    ring_frame, truth = _ring_and_truth()
    truth_subset = truth.filter(pl.col("symbol") != truth["symbol"][0])  # truth missing one ring symbol
    before = _breach_count()
    _record_point_ring_parity(ring_frame, truth_subset)
    assert _breach_count() == before, "an extra symbol in the ring (only_ring) must NOT record a breach"


@pytest.mark.parametrize("parity_flag", ["0", "1"])
def test_shadow_never_alters_served_output(monkeypatch: pytest.MonkeyPatch, parity_flag: str) -> None:
    """MONITORING-ONLY: the engine's served _latest_frame is byte-identical whether FP_POINT_RING_PARITY is on
    or off — the shadow records a metric, never changes values."""
    from quantlib.features.incremental import IncrementalEngine

    monkeypatch.setenv("FP_POINT_RING", "1")
    frames = build_frames(24, 130, 250, include_trades=True)
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    buf = frames["minute_agg"]
    minutes = sorted(buf["minute"].unique())

    def served(flag: str) -> dict[str, pl.DataFrame]:
        monkeypatch.setenv("FP_POINT_RING_PARITY", flag)
        engine = IncrementalEngine(groups)
        out = None
        for minute in minutes[-3:]:
            out = engine.step(buf.filter(pl.col("minute") <= minute))
        assert out is not None
        return out

    off = served("0")
    on = served(parity_flag)
    for name in off:
        a = off[name].sort("symbol")
        b = on[name].sort("symbol").select(a.columns)
        assert a.equals(b), f"{name}: FP_POINT_RING_PARITY={parity_flag} altered the served output"
