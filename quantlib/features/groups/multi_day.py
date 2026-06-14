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
    # Per-session cache of the daily features keyed by the daily-snapshot object id. The snapshot is fixed
    # for the whole trading day, so its derived daily features are identical every minute — compute once,
    # broadcast each minute (this recompute-every-minute over the full daily history was the group's cost).
    _daily_cache: tuple[int, pl.DataFrame, list[str]] | None = None

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

    def _daily(self, ctx: BatchContext) -> tuple[pl.DataFrame, list[str]]:
        """The per-(symbol, date) daily features (point-in-time as of the prior close). Shared by both
        compute() and compute_latest() — the SAME code; only the minute broadcast differs. Cached on the
        daily-snapshot identity so the (identical-all-day) daily features are computed once, not per minute."""
        source = ctx.frame("daily")
        cached = self._daily_cache
        if cached is not None and cached[0] == id(source):
            return cached[1], cached[2]
        daily = source.select(["symbol", "date", "close"]).sort(["symbol", "date"])
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
        result = daily.with_columns(exprs).select(["symbol", "date", *names])
        self._daily_cache = (id(source), result, names)
        return result, names

    def _broadcast(self, minutes: pl.DataFrame, daily: pl.DataFrame, names: list[str]) -> pl.DataFrame:
        minutes = minutes.with_columns(pl.col("minute").dt.date().alias("date"))
        return minutes.join(daily, on=["symbol", "date"], how="left").select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        daily, names = self._daily(ctx)
        return self._broadcast(ctx.frame("minute_agg").select(["symbol", "minute"]), daily, names)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Broadcast the daily features to ONLY the latest minute's rows (not all 390) — the daily
        computation is identical to compute()."""
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = minutes["minute"].max()
        daily, names = self._daily(ctx)
        return self._broadcast(minutes.filter(pl.col("minute") == latest), daily, names)
