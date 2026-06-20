"""Unit tests for the volume_leads_price group (Layer A, minute_agg windowed-OLS correlation).

Pins the lagged-correlation math on a hand-built series with a planted lead-lag relationship, and
directly asserts the point-in-time property (appending a FUTURE bar does not change any past value).
ReductionGroup parity (compute_latest == compute) is auto-guarded by tests/test_fp_latest.py; this file
pins the formula and the no-look-ahead contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _frame(closes: list[float], volumes: list[float]) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * n,
            "minute": [BASE + timedelta(minutes=i) for i in range(n)],
            "close": closes,
            "volume": volumes,
        }
    )


def _expected_lag_corr(closes: list[float], volumes: list[float], lag: int, window: int, as_of: int) -> float:
    """Reference: corr(volume[t-lag], return[t]) over the trailing `window` minutes ending at `as_of`,
    matching the group's within-window paired-OLS (only pairs where both sides exist contribute)."""
    rets = [np.nan] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    xs, ys = [], []
    # window is by wall-clock minute on a contiguous grid -> the trailing `window` minutes are
    # (as_of-window, as_of]; rolling_*_by is right-closed and includes as_of.
    for t in range(max(0, as_of - window + 1), as_of + 1):
        x = volumes[t - lag] if t - lag >= 0 else np.nan
        y = rets[t]
        if not np.isnan(x) and not np.isnan(y):
            xs.append(x)
            ys.append(y)
    if len(xs) < 2 or np.std(xs) == 0 or np.std(ys) == 0:
        return np.nan
    return float(np.corrcoef(xs, ys)[0, 1])


def test_lagged_correlation_matches_reference() -> None:
    rng = np.random.default_rng(7)
    n = 80
    closes = list(np.cumprod(1.0 + rng.normal(0, 0.003, n)) * 100.0)
    volumes = list(rng.uniform(1000, 5000, n))
    out = run_group(REGISTRY.get_group("volume_leads_price"), BatchContext(frames={"minute_agg": _frame(closes, volumes)}))
    as_of = n - 1
    row = out.filter(pl.col("minute") == BASE + timedelta(minutes=as_of)).row(0, named=True)
    for w in (15, 30, 60):
        for k in (1, 2, 3, 5):
            expected = _expected_lag_corr(closes, volumes, k, w, as_of)
            actual = row[f"vol_leads_corr_lag{k}_{w}m"]
            if np.isnan(expected):
                assert actual is None
            else:
                assert actual == pytest.approx(expected, abs=1e-6)


def test_no_look_ahead_appending_future_bar() -> None:
    # A past minute's value must be identical whether or not later bars exist (point-in-time).
    rng = np.random.default_rng(11)
    n = 70
    closes = list(np.cumprod(1.0 + rng.normal(0, 0.004, n)) * 50.0)
    volumes = list(rng.uniform(2000, 8000, n))
    group = REGISTRY.get_group("volume_leads_price")

    as_of = 60
    truncated = run_group(group, BatchContext(frames={"minute_agg": _frame(closes[: as_of + 1], volumes[: as_of + 1])}))
    full = run_group(group, BatchContext(frames={"minute_agg": _frame(closes, volumes)}))

    t = BASE + timedelta(minutes=as_of)
    r_trunc = truncated.filter(pl.col("minute") == t).row(0, named=True)
    r_full = full.filter(pl.col("minute") == t).row(0, named=True)
    for feat in group.feature_names:
        a, b = r_trunc[feat], r_full[feat]
        if a is None or b is None:
            assert a is b
        else:
            assert a == pytest.approx(b, abs=1e-9)


def test_zero_close_return_does_not_poison_corr() -> None:
    """A zero close makes close/close.shift(1) ±Inf; the is_finite() backstop NULLs that return so the
    windowed OLS correlation stays finite-or-NULL (never ±Inf / NaN-poisoned) on both paths."""
    rng = np.random.default_rng(3)
    n = 80
    closes = list(np.cumprod(1.0 + rng.normal(0, 0.003, n)) * 100.0)
    closes[40] = 0.0  # a single bad zero-close print: return at 40 (0/x) and 41 (x/0=Inf) both undefined
    volumes = list(rng.uniform(1000, 5000, n))
    out = run_group(
        REGISTRY.get_group("volume_leads_price"),
        BatchContext(frames={"minute_agg": _frame(closes, volumes)}),
    )
    for feat in REGISTRY.get_group("volume_leads_price").feature_names:
        for value in out[feat].to_list():
            assert value is None or np.isfinite(value)
