"""Price returns over many trailing minute windows (family: PRICE).

Simple and log close-to-close returns, point-in-time, all sessions. Time-based lags (the lagged()
helper) so they are correct on gappy minute grids.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    lagged,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 45, 60, 90, 120, 180)


@register
class PriceReturnGroup(FeatureGroup):
    name = "price_returns"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"ret_{w}m",
                    description=f"Simple close-to-close return over the trailing {w} minute(s), point-in-time as of the minute open.",
                    dtype="Float64",
                    valid_range=(-1.0, 5.0),
                    nan_policy="warmup",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"log_ret_{w}m",
                    description=f"Log close-to-close return ln(close/close_-{w}m) over the trailing {w} minute(s), point-in-time.",
                    dtype="Float64",
                    valid_range=(-5.0, 5.0),
                    nan_policy="warmup",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for w in WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
        exprs = []
        for w in WINDOWS:
            ratio = pl.col("close") / pl.col(f"_lag{w}")
            exprs.append((ratio - 1.0).cast(pl.Float64).alias(f"ret_{w}m"))
            exprs.append(ratio.log().cast(pl.Float64).alias(f"log_ret_{w}m"))
        names = [f"ret_{w}m" for w in WINDOWS] + [f"log_ret_{w}m" for w in WINDOWS]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: a return at T is a POINT lookup (close_T / close_{T-w}), not a window
        reduction — so just join the close from each lag minute, no rolling over the buffer. Same
        formula as compute(), parity-guarded."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        latest = frame["minute"].max()
        out = frame.filter(pl.col("minute") == latest).select(["symbol", pl.col("close").alias("_cT")])
        for w in WINDOWS:
            lag = frame.filter(pl.col("minute") == latest - dt.timedelta(minutes=w)).select(
                ["symbol", pl.col("close").alias(f"_c{w}")]
            )
            out = out.join(lag, on="symbol", how="left")
        exprs = []
        for w in WINDOWS:
            ratio = pl.col("_cT") / pl.col(f"_c{w}")
            exprs.append((ratio - 1.0).cast(pl.Float64).alias(f"ret_{w}m"))
            exprs.append(ratio.log().cast(pl.Float64).alias(f"log_ret_{w}m"))
        names = [f"ret_{w}m" for w in WINDOWS] + [f"log_ret_{w}m" for w in WINDOWS]
        return out.with_columns(exprs).with_columns(pl.lit(latest).alias("minute")).select(["symbol", "minute", *names])
