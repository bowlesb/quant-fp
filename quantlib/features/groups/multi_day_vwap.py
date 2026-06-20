"""Multi-day VWAP features from the daily cache (family: MULTI_DAY, Layer A).

Where the prior close sits versus the volume-weighted average price over the last week / two weeks /
month / quarter / half-year. The N-day VWAP = sum(daily_vwap * daily_volume) / sum(daily_volume) over
the last N COMPLETED trading days (point-in-time as of the prior close, never today's incomplete bar),
broadcast across the day's minutes. Same daily frame live and backfill → parity holds (mirrors
multi_day / prior_day). Complements the intraday VWAP-deviation (price_volume) at the daily horizon.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    daily_snapshot_token,
)
from quantlib.features.registry import register

VWAP_DAYS: tuple[int, ...] = (5, 10, 20, 60, 120)  # ~week, 2wk, month, quarter, half-year


@register
class MultiDayVwapGroup(FeatureGroup):
    name = "multi_day_vwap"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MULTI_DAY
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close", "volume", "vwap")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )
    # Daily features are identical every minute (the snapshot is fixed all day) — cache them on the
    # snapshot identity so they are computed once, not re-derived over the full daily history per minute.

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for days in VWAP_DAYS:
            specs.append(
                FeatureSpec(name=f"dist_from_vwap_{days}d", description=f"Prior close relative to the {days}-day volume-weighted average price (close/vwap_{days}d - 1), point-in-time.",
                            dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"above_vwap_{days}d", description=f"1.0 when the prior close is above the {days}-day volume-weighted average price, else 0.0.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A")
            )
        return specs

    def _daily(self, ctx: BatchContext) -> tuple[pl.DataFrame, list[str]]:
        """Per-(symbol, date) daily VWAP-distance features, cached on the daily-snapshot identity."""
        source = ctx.frame("daily")
        names = [spec.name for spec in self.declare()]
        result = self.session_cache.get(
            daily_snapshot_token(source), lambda: self._compute_daily(source, names)
        )
        return result, names

    def _compute_daily(self, source: pl.DataFrame, names: list[str]) -> pl.DataFrame:
        daily = source.select(["symbol", "date", "close", "volume", "vwap"]).sort(["symbol", "date"])
        # shift by 1 so the rolling windows end at the PRIOR completed day (never today's incomplete bar)
        daily = daily.with_columns(
            [
                (pl.col("vwap") * pl.col("volume")).shift(1).over("symbol").alias("_pv1"),
                pl.col("volume").shift(1).over("symbol").alias("_vol1"),
                pl.col("close").shift(1).over("symbol").alias("_pc"),
            ]
        )
        exprs = []
        for days in VWAP_DAYS:
            sum_pv = pl.col("_pv1").rolling_sum(window_size=days).over("symbol")
            sum_vol = pl.col("_vol1").rolling_sum(window_size=days).over("symbol")
            vwap_n = sum_pv / sum_vol
            exprs.append((pl.col("_pc") / vwap_n - 1.0).cast(pl.Float64).alias(f"dist_from_vwap_{days}d"))
            exprs.append((pl.col("_pc") > vwap_n).cast(pl.Float64).alias(f"above_vwap_{days}d"))
        return daily.with_columns(exprs).select(["symbol", "date", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        daily, names = self._daily(ctx)
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"]).with_columns(pl.col("minute").dt.date().alias("date"))
        return minutes.join(daily, on=["symbol", "date"], how="left").select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Same code as compute(), run on a minute_agg restricted to the latest minute (broadcast to one
        minute, not all 390)."""
        minute_agg = ctx.frame("minute_agg")
        latest = minute_agg["minute"].max()
        sub = BatchContext(frames={**ctx.frames, "minute_agg": minute_agg.filter(pl.col("minute") == latest)})
        return self.compute(sub)
