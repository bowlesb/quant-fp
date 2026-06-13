"""Momentum / trend-consistency features from per-minute close (family: MOMENTUM, Layer A)."""
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

WINDOWS: tuple[int, ...] = (5, 15, 30, 60)


@register
class MomentumGroup(FeatureGroup):
    name = "momentum"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"up_ratio_{w}m", description=f"Fraction of the trailing {w} minutes with a positive one-minute return (0-1).",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"mean_abs_ret_{w}m", description=f"Mean absolute one-minute return over the trailing {w} minutes (choppiness).",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="warmup", layer="A")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        frame = frame.with_columns(
            [
                (pl.col("close") / pl.col("_prev") - 1.0).alias("_ret"),
                ((pl.col("close") / pl.col("_prev") - 1.0) > 0.0).cast(pl.Float64).alias("_up"),
            ]
        )
        exprs = []
        for w in WINDOWS:
            exprs.append(pl.col("_up").rolling_mean_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"up_ratio_{w}m"))
            exprs.append(pl.col("_ret").abs().rolling_mean_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"mean_abs_ret_{w}m"))
        names = [f"{f}_{w}m" for w in WINDOWS for f in ("up_ratio", "mean_abs_ret")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
