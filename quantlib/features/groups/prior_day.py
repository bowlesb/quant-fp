"""Prior-day level features: where today's price sits versus yesterday (family: MULTI_DAY, Layer A).

The overnight gap, the classic floor-trader pivots (P / R1-2 / S1-2) from the prior day's OHLC, and
the current close's distance from yesterday's high / low / close and from each pivot. All anchored to
the LAST COMPLETED daily bar (D-1), then broadcast across today's minutes — point-in-time, never
using today's incomplete daily bar for the levels. Lives in the daily cache; the same daily frame
feeds live and backfill, so parity holds. Split caveat (shared with multi_day): the daily cache is
split-adjusted while intraday close is raw, so a same-day split would skew the level until the
corporate-actions layer reconciles it.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.daily_snapshot_group import DailySnapshotGroup
from quantlib.features.registry import register

PIVOTS = ("p", "r1", "s1", "r2", "s2")


@register
class PriorDayGroup(DailySnapshotGroup):
    name = "prior_day"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MULTI_DAY
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "open", "high", "low", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
    )
    # A.2: the snapshot holds prior-day LEVELS; the features mix a level with the at-T minute close, so carry
    # ``close`` into the broadcast and derive the close-relative distances via ``broadcast_exprs``.
    minute_columns = ("close",)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(name="gap_open", description="Overnight gap: today's daily open relative to the prior day's close (open/prev_close - 1).",
                        dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A"),
            FeatureSpec(name="dist_from_prior_high", description="Current close relative to the prior day's high (close/prev_high - 1).",
                        dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A"),
            FeatureSpec(name="dist_from_prior_low", description="Current close relative to the prior day's low (close/prev_low - 1).",
                        dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A"),
            FeatureSpec(name="dist_from_prior_close", description="Current close relative to the prior day's close (close/prev_close - 1).",
                        dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A"),
            FeatureSpec(name="above_pivot", description="1.0 when the current close is above the prior-day floor pivot P, else 0.0.",
                        dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A"),
        ]
        labels = {"p": "the pivot P", "r1": "resistance R1", "s1": "support S1", "r2": "resistance R2", "s2": "support S2"}
        for pivot in PIVOTS:
            specs.append(
                FeatureSpec(name=f"dist_from_pivot_{pivot}", description=f"Current close relative to {labels[pivot]} from the prior day's OHLC (close/level - 1).",
                            dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A")
            )
        return specs

    def daily_snapshot(self, source: pl.DataFrame, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, date) prior-day LEVELS (gap, prior H/L/C, pivots) — depend only on the daily snapshot.
        The close-relative distances are applied per minute by ``broadcast_exprs`` (A.2)."""
        daily = source.select(["symbol", "date", "open", "high", "low", "close"]).sort(["symbol", "date"])
        prev_high = pl.col("high").shift(1).over("symbol")
        prev_low = pl.col("low").shift(1).over("symbol")
        prev_close = pl.col("close").shift(1).over("symbol")
        pivot = (prev_high + prev_low + prev_close) / 3.0
        span = prev_high - prev_low
        daily = daily.with_columns(
            [
                (pl.col("open") / prev_close - 1.0).alias("_gap_open"),
                prev_high.alias("_prev_high"),
                prev_low.alias("_prev_low"),
                prev_close.alias("_prev_close"),
                pivot.alias("_p"),
                (2.0 * pivot - prev_low).alias("_r1"),
                (2.0 * pivot - prev_high).alias("_s1"),
                (pivot + span).alias("_r2"),
                (pivot - span).alias("_s2"),
            ]
        )
        level_cols = ["_gap_open", "_prev_high", "_prev_low", "_prev_close", *[f"_{p}" for p in PIVOTS]]
        return daily.select(["symbol", "date", *level_cols])

    def broadcast_exprs(self) -> list[pl.Expr]:
        """The close-relative feature expressions over the broadcast frame (prior-day levels joined onto each
        minute + the at-T ``close``). The SAME exprs run for all minutes (backfill) and the latest minute
        (live), so live == backfill by construction."""
        exprs = [
            pl.col("_gap_open").cast(pl.Float64).alias("gap_open"),
            (pl.col("close") / pl.col("_prev_high") - 1.0).cast(pl.Float64).alias("dist_from_prior_high"),
            (pl.col("close") / pl.col("_prev_low") - 1.0).cast(pl.Float64).alias("dist_from_prior_low"),
            (pl.col("close") / pl.col("_prev_close") - 1.0).cast(pl.Float64).alias("dist_from_prior_close"),
            (pl.col("close") > pl.col("_p")).cast(pl.Float64).alias("above_pivot"),
        ]
        for pivot_name in PIVOTS:
            exprs.append((pl.col("close") / pl.col(f"_{pivot_name}") - 1.0).cast(pl.Float64).alias(f"dist_from_pivot_{pivot_name}"))
        return exprs
