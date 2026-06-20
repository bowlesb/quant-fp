"""Adversarial-audit repro tests (BatteryAudit, 2026-06-19).

Each test below DEMONSTRATES a confirmed way the harness could be fooled / contradicts its own
stated anti-cheat discipline. They are marked xfail(strict=True) so they document the defect
without breaking the suite; flip to a passing assertion once the fix lands.

Findings (see the audit report for full context):
  F1 — intraday `_forward_excess` applies NO $1 price-integrity floor on the FORWARD leg, while the
       module docstring + daily path claim the floor is on BOTH legs. A penny-print 30m forward
       leaks a ~-100% return into the cross-sectional label.
  F2 — the EOD `up_down_market` conditioner selects rows on `up_market_day` = sign of TODAY's
       open->close median, which is unknown at the 09:35 EOD entry (a look-ahead in one battery
       cell). Features are shifted prior-day for EOD; the conditioner mask is not.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.battery.panel import _forward_excess
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon
from quantlib.battery.strategy import CrossSectionalLS, _ratio_with_floor


@pytest.mark.xfail(reason="F1: intraday forward leg is not $1-floored (audit finding)", strict=True)
def test_intraday_forward_leg_applies_dollar_floor() -> None:
    """A penny-print FORWARD bar must be nulled the same way the daily path nulls it via
    `_ratio_with_floor`. Today it leaks a ~-100% return into the label."""
    day = dt.date(2025, 1, 6)
    t0 = dt.datetime(day.year, day.month, day.day, 14, 35, tzinfo=dt.timezone.utc)
    t1 = t0 + dt.timedelta(minutes=30)
    rows = []
    for i in range(21):  # >= MIN_CROSS_SECTION breadth so the minute is graded
        rows.append({"symbol": f"N{i}", "ts": t0, "close": 50.0})
        rows.append({"symbol": f"N{i}", "ts": t1, "close": 50.0 * (1 + 0.001 * (i - 10))})
    rows.append({"symbol": "PENNY", "ts": t0, "close": 50.0})   # entry $50 passes the entry floor
    rows.append({"symbol": "PENNY", "ts": t1, "close": 0.02})    # forward $0.02 penny print
    bars = pl.DataFrame(rows).with_columns(pl.col("ts").dt.replace_time_zone("UTC"))
    fwd = _forward_excess(bars, pl.Series([t0]), 30)
    penny_label = fwd.filter(pl.col("symbol") == "PENNY")["fwd_30m"].to_list()[0]
    # the daily path would floor this to NaN:
    assert np.isnan(_ratio_with_floor(np.array([0.02]), np.array([50.0]))[0])
    # the intraday path SHOULD too — this is the assertion that currently fails:
    assert penny_label is None or np.isnan(penny_label), (
        f"penny-print forward leg leaked label {penny_label} (no $1 floor on the forward leg)"
    )


@pytest.mark.xfail(reason="F2: EOD up_down_market conditioner reads same-day regime (audit finding)", strict=True)
def test_eod_conditioner_is_point_in_time() -> None:
    """`up_market_day` is the sign of today's open->close median — a FUTURE quantity at the 09:35 EOD
    entry. The EOD conditioner mask must not equal that same-day array (it must be prior-day, like the
    features are). Today it does, so the eod|up_down_market cell conditions on look-ahead."""
    from quantlib.battery.panel import Panel  # local import keeps the repro self-contained

    n = 30
    sc = (np.arange(n) % 5).astype(np.int64)
    mn = np.full(n, int(dt.datetime(2025, 1, 6, 19, 59, tzinfo=dt.timezone.utc).timestamp() * 1e9), dtype=np.int64)
    up = np.array([True] * (n // 2) + [False] * (n - n // 2))
    panel = Panel(
        symbol_code=sc, symbol_names=[f"S{i}" for i in range(5)], minute_epoch=mn,
        feature_names=["f0"], feature_matrix=np.zeros((n, 1)),
        entry_close=np.full(n, 50.0), half_spread_bps=np.full(n, 3.0),
        high=np.full(n, 51.0), low=np.full(n, 49.0), volume=np.full(n, 1e6),
        extra={
            "exec_0935": np.full(n, 49.0), "rth_close": np.full(n, 50.0),
            "exit_overnight": np.full(n, 50.0), "exit_2d": np.full(n, 50.0), "exit_3d": np.full(n, 50.0),
            "rth_dollar_vol": np.full(n, 1e8), "up_market_day": up,
        },
        cadence="daily",
    )
    strat = CrossSectionalLS(ArchetypeSpec("cross_sectional_ls", Horizon.EOD, Conditioner.UP_DOWN_MARKET))
    mask = strat._conditioner_mask(panel)
    assert not np.array_equal(mask, up), "EOD conditioner selects on today's realized direction (look-ahead)"
