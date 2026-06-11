"""Tests for forward-return label construction."""
import math
from datetime import datetime, timedelta, timezone

from quantlib.labels import (
    cross_sectional_excess,
    forward_return_series,
    horizon_name,
)

BASE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)


def test_forward_return_uses_timestamped_lookup() -> None:
    closes = {BASE + timedelta(minutes=i): 100.0 + i for i in range(5)}
    fwd = forward_return_series(closes, horizon_minutes=2)
    # at t0: close 100 -> t2 close 102 => 0.02
    assert abs(fwd[BASE] - (102.0 / 100.0 - 1.0)) < 1e-12
    # last two timestamps have no +2m bar -> NaN
    assert math.isnan(fwd[BASE + timedelta(minutes=4)])


def test_forward_return_handles_gap() -> None:
    # missing minute 1: the +1m horizon from t0 must be NaN, not silently t2
    closes = {BASE: 100.0, BASE + timedelta(minutes=2): 110.0}
    fwd = forward_return_series(closes, horizon_minutes=1)
    assert math.isnan(fwd[BASE])


def test_cross_sectional_excess_demeans_by_median() -> None:
    returns = {"A": 0.03, "B": 0.01, "C": -0.01}  # median 0.01
    excess = cross_sectional_excess(returns)
    assert abs(excess["A"] - 0.02) < 1e-12
    assert abs(excess["B"] - 0.0) < 1e-12
    assert abs(excess["C"] - (-0.02)) < 1e-12


def test_cross_sectional_excess_ignores_nan() -> None:
    returns = {"A": 0.02, "B": math.nan, "C": -0.02}  # median over valid = 0
    excess = cross_sectional_excess(returns)
    assert abs(excess["A"] - 0.02) < 1e-12
    assert math.isnan(excess["B"])


def test_horizon_name() -> None:
    assert horizon_name(30) == "fwd_30m"


def test_cross_sectional_excess_breadth_floor() -> None:
    from quantlib.labels import cross_sectional_excess as cse
    assert math.isnan(cse({"A": 0.01})["A"])                 # 1 name < default floor(2)
    assert all(math.isnan(v) for v in cse({"A": 0.01, "B": 0.02}, min_members=5).values())
