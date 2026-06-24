"""Calendar-event proximity features from the minute timestamp (family: CALENDAR, Layer A).

Pure deterministic functions of the ET timestamp — no lookback, no external calendar — so parity is
trivially exact and buffer-independent. Encodes structural calendar effects: options-expiry / triple-
witching Fridays, quarter-end months, and position within the month, which drive recurring flows.
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

QUARTER_END_MONTHS = [3, 6, 9, 12]


@register
class CalendarEventsGroup(FeatureGroup):
    name = "calendar_events"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CALENDAR
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="day_of_month_norm",
                description="Calendar day of month in ET divided by 31 (position through the month, 0-1).",
                dtype="Float64",
                valid_range=(0.0, 1.04),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="week_of_month",
                description="Week of the month in ET (1-5), as ceil(day_of_month / 7).",
                dtype="Float64",
                valid_range=(1.0, 5.0),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_opex_day",
                description="1.0 when the bar is on monthly options-expiration Friday (the third Friday of the month) in ET, else 0.0.",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_triple_witching",
                description="1.0 on quarterly triple-witching (third Friday of March/June/September/December) in ET, else 0.0.",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_quarter_end_month",
                description="1.0 when the bar falls in a quarter-end month (Mar/Jun/Sep/Dec) in ET, else 0.0.",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_first_week",
                description="1.0 when the ET calendar day of month is 7 or earlier (first week), else 0.0.",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_last_week",
                description="1.0 when the ET calendar day of month is 22 or later (last week, month-end window), else 0.0.",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
        ]

    def exprs(self) -> list[pl.Expr]:
        """The feature column expressions, shared by compute() and the consolidated point-in-time emit."""
        et = pl.col("minute").dt.convert_time_zone("America/New_York")
        day = et.dt.day().cast(pl.Int32)
        month = et.dt.month().cast(pl.Int32)
        is_friday = et.dt.weekday() == 5
        is_opex = is_friday & (day >= 15) & (day <= 21)
        return [
            (day.cast(pl.Float64) / 31.0).alias("day_of_month_norm"),
            (((day - 1) // 7) + 1).cast(pl.Float64).alias("week_of_month"),
            is_opex.cast(pl.Float64).alias("is_opex_day"),
            (is_opex & month.is_in(QUARTER_END_MONTHS)).cast(pl.Float64).alias("is_triple_witching"),
            month.is_in(QUARTER_END_MONTHS).cast(pl.Float64).alias("is_quarter_end_month"),
            (day <= 7).cast(pl.Float64).alias("is_first_week"),
            (day >= 22).cast(pl.Float64).alias("is_last_week"),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return (
            ctx.frame("minute_agg")
            .select(["symbol", "minute"])
            .with_columns(self.exprs())
            .select(
                [
                    "symbol",
                    "minute",
                    "day_of_month_norm",
                    "week_of_month",
                    "is_opex_day",
                    "is_triple_witching",
                    "is_quarter_end_month",
                    "is_first_week",
                    "is_last_week",
                ]
            )
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        # Per-row function of the minute's calendar date — no cross-minute window, so compute ONLY the latest
        # minute instead of the whole buffer (the default compute_latest). Parity-guarded by test_fp_latest.
        return self.compute_latest_point_in_time(ctx)
