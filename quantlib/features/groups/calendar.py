"""Calendar / session features (family: CALENDAR, Layer A).

Pure functions of the minute's exchange timestamp (converted to ET) — identical stream vs backfill
by construction, so parity is trivially exact. Encodes time-of-day and session, which condition
many intraday effects.
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
from quantlib.features.session import CLOSE_MINUTE, OPEN_MINUTE


@register
class CalendarGroup(FeatureGroup):
    name = "calendar"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CALENDAR
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="minute_of_day_et",
                description="Minutes since ET midnight for this bar (0-1439); encodes time of day.",
                dtype="Float64",
                valid_range=(0.0, 1440.0),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="day_of_week",
                description="ISO weekday of the bar in ET (Monday=1 .. Sunday=7).",
                dtype="Float64",
                valid_range=(1.0, 7.0),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="minutes_since_open",
                description="Minutes since the 09:30 ET regular open (negative during pre-market).",
                dtype="Float64",
                valid_range=(-570.0, 870.0),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_regular_session",
                description="1.0 if within the 09:30-16:00 ET regular session, else 0.0 (extended hours).",
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="none",
                layer="A",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        et = pl.col("minute").dt.convert_time_zone("America/New_York")
        # cast to Int32 BEFORE *60 — dt.hour() is Int8 and 10*60 overflows it (600 -> 88).
        minute_of_day = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        return ctx.frame("minute_agg").select(["symbol", "minute"]).with_columns(
            [
                minute_of_day.cast(pl.Float64).alias("minute_of_day_et"),
                et.dt.weekday().cast(pl.Float64).alias("day_of_week"),
                (minute_of_day - OPEN_MINUTE).cast(pl.Float64).alias("minutes_since_open"),
                ((minute_of_day >= OPEN_MINUTE) & (minute_of_day < CLOSE_MINUTE)).cast(pl.Float64).alias(
                    "is_regular_session"
                ),
            ]
        ).select(
            ["symbol", "minute", "minute_of_day_et", "day_of_week", "minutes_since_open", "is_regular_session"]
        )
