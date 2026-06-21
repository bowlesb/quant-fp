"""Multi-timescale tests: daily (multi-day) features are point-in-time + broadcast correctly, and
time-based rolling windows (realized_vol, volume z-score) are correct on GAPPY minute grids.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features.base import BatchContext
from quantlib.features.engine import run_group
from quantlib.features.reduction_anchor import _RTH_MINUTES_PER_DAY, attach_volume_anchor
from quantlib.features.registry import REGISTRY

DAY = date(2026, 6, 12)


def _with_volume_anchor(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach volume's per-symbol centering anchor, as production capture/backfill does where minute_agg is
    built. Synthesize a daily snapshot at DAILY-TOTAL scale so ``attach_volume_anchor`` re-derives the
    per-minute anchor exactly as in prod."""
    daily = (
        frame.group_by("symbol", maintain_order=True)
        .agg((pl.col("volume").last() * _RTH_MINUTES_PER_DAY).alias("volume"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_volume_anchor(frame, daily)


def test_volume_group() -> None:
    base = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    n = 40
    frame = _with_volume_anchor(
        pl.DataFrame(
            {
                "symbol": ["AAA"] * n,
                "minute": [base + timedelta(minutes=i) for i in range(n)],
                "close": [100.0 + i * 0.1 for i in range(n)],
                "volume": [1000.0 + (i % 7) * 100 for i in range(n)],
            }
        )
    )
    out = run_group(REGISTRY.get_group("volume"), BatchContext(frames={"minute_agg": frame}))
    row = out.filter(pl.col("minute") == base + timedelta(minutes=10)).row(0, named=True)
    assert row["dollar_volume_1m"] == pytest.approx((100.0 + 1.0) * (1000.0 + (10 % 7) * 100))
    assert out.filter(pl.col("volume_zscore_30m").is_not_null()).height > 0


def _daily(closes: list[float]) -> pl.DataFrame:
    # one row per trading day, ending the day BEFORE DAY (so DAY itself has prior history)
    days = [DAY - timedelta(days=len(closes) - i) for i in range(len(closes))]
    return pl.DataFrame({"symbol": ["AAA"] * len(closes), "date": days, "close": closes})


def _minutes_on(day: date, n: int = 5) -> pl.DataFrame:
    base = datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc)
    return pl.DataFrame({"symbol": ["AAA"] * n, "minute": [base + timedelta(minutes=i) for i in range(n)]})


def test_multiday_point_in_time_and_broadcast() -> None:
    # 22 prior trading days of close, plus DAY itself with a WILD close that must be IGNORED (PIT)
    closes = [100.0 + i for i in range(22)]  # ... last prior day's close = 121
    daily = pl.concat([_daily(closes), pl.DataFrame({"symbol": ["AAA"], "date": [DAY], "close": [9999.0]})])
    minutes = _minutes_on(DAY, n=5)
    group = REGISTRY.get_group("multi_day_returns")
    out = run_group(group, BatchContext(frames={"daily": daily, "minute_agg": minutes}))

    # broadcast: every minute of DAY has the same daily value
    assert out["daily_return_5d"].n_unique() == 1
    # point-in-time: uses close[D-1]=121 / close[D-6]=116 - 1, NOT the wild 9999 of DAY
    row = out.row(0, named=True)
    assert row["daily_return_5d"] == (121.0 / 116.0 - 1.0)
    assert row["daily_return_1d"] == (121.0 / 120.0 - 1.0)


def test_realized_vol_is_time_based_on_gappy_grid() -> None:
    # returns then a 30-min gap: a POSITIONAL 5-window would wrongly mix across the gap; time-based
    # "5m" must only use minutes within 5 wall-clock minutes.
    base = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    offsets = [0, 1, 2, 3, 4, 40, 41]  # a big gap before 40
    frame = pl.DataFrame(
        {
            "symbol": ["AAA"] * len(offsets),
            "minute": [base + timedelta(minutes=o) for o in offsets],
            "high": [100.5] * len(offsets),
            "low": [99.5] * len(offsets),
            "close": [100.0, 101.0, 100.0, 102.0, 101.0, 130.0, 131.0],
        }
    )
    out = run_group(REGISTRY.get_group("volatility"), BatchContext(frames={"minute_agg": frame})).sort("minute")
    # the minute at offset 40 has only one prior return within 5m (offset 41 not yet) -> vol is null
    # or based only on the 40/41 pair, NOT contaminated by the 0-4 block across the 35-min gap.
    at_40 = out.filter(pl.col("minute") == base + timedelta(minutes=40)).row(0, named=True)
    assert at_40["realized_vol_5m"] is None  # no return within the prior 5 minutes (gap)
