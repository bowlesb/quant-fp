"""Price returns over many trailing minute windows (family: PRICE).

Simple and log close-to-close returns, point-in-time, all sessions. Time-based lags (the lagged()
helper) so they are correct on gappy minute grids.
"""
from __future__ import annotations

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

WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 45, 60, 90, 120)


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
