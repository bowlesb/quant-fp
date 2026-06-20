"""residual_analysis (ReductionGroup, Lever-2) — value-identity + parity tests.

residual_analysis was migrated from a hand-written rolling FeatureGroup to a ReductionGroup that reads the
shared ``resid_std`` OLS stat (computed from the same six paired OLS sums by the polars/numpy/rust twins). These
tests pin its output against an INDEPENDENT numpy OLS-residual reference (the math ground truth, not the old
implementation) and assert the generated live form matches the backfill form within the declared tolerance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl

from quantlib.features import BatchContext, REGISTRY
from quantlib.features.groups.residual_analysis import RESID_TOL, WINDOWS

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _frame(closes: list[float], symbol: str = "AAA") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(closes))],
            "close": closes,
        }
    )


def _ref_residual_std(closes: np.ndarray) -> float:
    """Independent reference: OLS residual std (percent of mean) of the WHOLE series, the math definition the
    group computes for the trailing window ending at the last minute. Returns NaN for the group's null cases
    (n<4, flat x is impossible for a contiguous grid, or a near-perfect fit below the relative floor)."""
    n = len(closes)
    if n < 4:
        return float("nan")
    x = np.arange(n, dtype=float)  # contiguous minute grid -> x = 0..n-1 (origin-invariant)
    coeffs = np.polyfit(x, closes, 1)
    resid = closes - np.polyval(coeffs, x)
    resid_var = float(np.sum(resid * resid) / n)
    mean_close = float(closes.mean())
    if resid_var <= (1e-6 * mean_close) ** 2:  # REL_RESID_FLOOR² · ȳ²
        return float("nan")
    return float(np.sqrt(resid_var) / mean_close * 100.0)


def test_residual_std_matches_numpy_reference() -> None:
    """The 5m residual_std at the last minute equals an independent numpy OLS-residual computation."""
    rng = np.random.default_rng(11)
    closes = (100.0 + np.cumsum(rng.normal(0.0, 0.3, 30))).tolist()
    group = REGISTRY.get_group("residual_analysis")
    out = group.compute(BatchContext(frames={"minute_agg": _frame(closes)}))
    last = out.filter(pl.col("minute") == out["minute"].max()).row(0, named=True)
    for w in (5, 10, 15, 20, 30):
        ref = _ref_residual_std(np.array(closes[-w:]))
        got = last[f"residual_std_{w}m"]
        assert got is not None and abs(got - ref) < RESID_TOL, f"w={w}: {got} vs ref {ref}"


def test_residual_std_near_linear_is_nulled() -> None:
    """A near-perfectly linear path -> residual variance below the relative floor -> null (not a noise reading)."""
    closes = [100.0 + 0.5 * i for i in range(30)]  # exact line
    group = REGISTRY.get_group("residual_analysis")
    out = group.compute(BatchContext(frames={"minute_agg": _frame(closes)}))
    last = out.filter(pl.col("minute") == out["minute"].max()).row(0, named=True)
    for w in WINDOWS:
        assert last[f"residual_std_{w}m"] is None


def test_residual_std_warmup_below_min_points_is_null() -> None:
    """Fewer than MIN_POINTS (4) closes in a window -> null (warmup, no meaningful residual distribution)."""
    closes = [100.0, 101.0, 99.0]  # only 3 points
    group = REGISTRY.get_group("residual_analysis")
    out = group.compute(BatchContext(frames={"minute_agg": _frame(closes)}))
    last = out.filter(pl.col("minute") == out["minute"].max()).row(0, named=True)
    assert last["residual_std_5m"] is None


def test_residual_std_latest_matches_backfill_on_deep_buffer() -> None:
    """The generated live compute_latest equals the backfill compute().last within the declared tolerance, on a
    buffer DEEPER than the deepest window (the kernel-sums-vs-rolling-sums parity every ReductionGroup holds).
    """
    rng = np.random.default_rng(7)
    closes = (100.0 + np.cumsum(rng.normal(0.0, 0.4, 90))).tolist()
    ctx = BatchContext(frames={"minute_agg": _frame(closes)})
    group = REGISTRY.get_group("residual_analysis")
    backfill = group.compute(ctx)
    backfill_last = backfill.filter(pl.col("minute") == backfill["minute"].max()).row(0, named=True)
    live = group.compute_latest(ctx).row(0, named=True)
    for w in WINDOWS:
        bf = backfill_last[f"residual_std_{w}m"]
        lv = live[f"residual_std_{w}m"]
        assert (bf is None) == (lv is None), f"w={w}: null mismatch bf={bf} lv={lv}"
        if bf is not None:
            assert abs(bf - lv) < RESID_TOL, f"w={w}: backfill {bf} vs live {lv}"
