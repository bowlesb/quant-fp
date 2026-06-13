"""Multi-day features from DAILY bars over many horizons (family: MULTI_DAY).

POINT-IN-TIME: at any minute of day D the most recent COMPLETED daily bar is D-1's, so everything is
computed from the prior-close series (close[D-1] back), never today's incomplete bar, then broadcast
across the day's minutes. Lives in the DAILY cache. Positional shift on the daily frame is a true
trading-day lag (one row per trading day); same daily frame live & backfill → parity holds.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

DAY_WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60, 90, 120, 180, 240)
VOL_DAYS: tuple[int, ...] = (5, 10, 20, 30, 60)
HIGH_DAYS: tuple[int, ...] = (10, 20, 60, 120, 250)


@register
class MultiDayReturnGroup(FeatureGroup):
    name = "multi_day_returns"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MULTI_DAY
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in DAY_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"daily_return_{w}d",
                    description=f"Return over the last {w} completed trading day(s), point-in-time as of the prior close.",
                    dtype="Float64",
                    valid_range=(-1.0, 20.0),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        for w in VOL_DAYS:
            specs.append(
                FeatureSpec(
                    name=f"daily_vol_{w}d",
                    description=f"Standard deviation of daily returns over the last {w} completed trading days (point-in-time).",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        for w in HIGH_DAYS:
            specs.append(
                FeatureSpec(
                    name=f"dist_from_{w}d_high",
                    description=f"Prior close relative to its {w}-day high (close[D-1]/max - 1), point-in-time; <= 0.",
                    dtype="Float64",
                    valid_range=(-1.0, 0.01),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        daily = ctx.frame("daily").select(["symbol", "date", "close"]).sort(["symbol", "date"])
        # as-of close = the PRIOR completed day's close (never today's incomplete bar)
        daily = daily.with_columns(pl.col("close").shift(1).over("symbol").alias("_asof"))
        daily = daily.with_columns((pl.col("_asof") / pl.col("_asof").shift(1).over("symbol") - 1.0).alias("_dret"))
        exprs = []
        for w in DAY_WINDOWS:
            exprs.append((pl.col("_asof") / pl.col("_asof").shift(w).over("symbol") - 1.0).cast(pl.Float64).alias(f"daily_return_{w}d"))
        for w in VOL_DAYS:
            exprs.append(pl.col("_dret").rolling_std(window_size=w).over("symbol").cast(pl.Float64).alias(f"daily_vol_{w}d"))
        for w in HIGH_DAYS:
            exprs.append((pl.col("_asof") / pl.col("_asof").rolling_max(window_size=w).over("symbol") - 1.0).cast(pl.Float64).alias(f"dist_from_{w}d_high"))
        names = (
            [f"daily_return_{w}d" for w in DAY_WINDOWS]
            + [f"daily_vol_{w}d" for w in VOL_DAYS]
            + [f"dist_from_{w}d_high" for w in HIGH_DAYS]
        )
        daily = daily.with_columns(exprs)
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"]).with_columns(pl.col("minute").dt.date().alias("date"))
        return minutes.join(daily.select(["symbol", "date", *names]), on=["symbol", "date"], how="left").select(
            ["symbol", "minute", *names]
        )
