"""Multi-day features from DAILY bars (family: MULTI_DAY) — the multi-timescale horizon.

POINT-IN-TIME: at any minute of day D the most recent COMPLETED daily bar is D-1's, so a w-day
return as of day D = close[D-1] / close[D-1-w] - 1 — it NEVER uses day D's own (incomplete) bar.
The value is constant across all of day D's minutes (broadcast). Lives in the DAILY cache, not the
minute buffer (memory: one row per symbol-day, not per minute).

Positional shift on the daily frame is correct here (unlike intraday): the daily history has exactly
one row per TRADING day, so shift(w) is a true w-trading-day lag. Same daily frame live & backfill
→ parity holds.
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

DAY_WINDOWS = (1, 5, 10, 20)  # last day / week / two weeks / month


@register
class MultiDayReturnGroup(FeatureGroup):
    name = "multi_day_returns"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MULTI_DAY
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),  # the grid to broadcast onto
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"daily_return_{w}d",
                description=(
                    f"Return over the last {w} completed trading day(s), point-in-time as of the "
                    f"prior close (close[D-1]/close[D-1-{w}] - 1); constant across the day's minutes."
                ),
                dtype="Float64",
                valid_range=(-1.0, 10.0),
                nan_policy="warmup",
                layer="A",
            )
            for w in DAY_WINDOWS
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        daily = ctx.frame("daily").select(["symbol", "date", "close"]).sort(["symbol", "date"])
        # as-of close = the PRIOR completed day's close (never today's incomplete bar)
        daily = daily.with_columns(pl.col("close").shift(1).over("symbol").alias("_asof"))
        daily = daily.with_columns(
            [
                (pl.col("_asof") / pl.col("_asof").shift(w).over("symbol") - 1.0).cast(pl.Float64).alias(f"daily_return_{w}d")
                for w in DAY_WINDOWS
            ]
        )
        features = [f"daily_return_{w}d" for w in DAY_WINDOWS]
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"]).with_columns(
            pl.col("minute").dt.date().alias("date")
        )
        return minutes.join(daily.select(["symbol", "date", *features]), on=["symbol", "date"], how="left").select(
            ["symbol", "minute", *features]
        )
