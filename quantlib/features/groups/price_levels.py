"""Price-level features: where close sits within its recent range (family: PRICE, Layer A).

Time-anchored rolling high/low so the window is wall-clock, correct on gappy grids.
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

WINDOWS: tuple[int, ...] = (15, 30, 60)


@register
class PriceLevelGroup(FeatureGroup):
    name = "price_levels"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "high", "low")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"position_in_range_{w}m",
                    description=f"Where close sits in its trailing {w}-minute high-low range: (close - min_low) / (max_high - min_low).",
                    dtype="Float64",
                    valid_range=(-0.01, 1.01),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"dist_from_high_{w}m",
                    description=f"Close relative to the trailing {w}-minute high (close / max_high - 1); <= 0.",
                    dtype="Float64",
                    valid_range=(-1.0, 0.01),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"dist_from_low_{w}m",
                    description=f"Close relative to the trailing {w}-minute low (close / min_low - 1); >= 0.",
                    dtype="Float64",
                    valid_range=(-0.01, 5.0),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "high", "low"]).sort(["symbol", "minute"])
        exprs = []
        for w in WINDOWS:
            high_w = pl.col("high").rolling_max_by("minute", window_size=f"{w}m").over("symbol")
            low_w = pl.col("low").rolling_min_by("minute", window_size=f"{w}m").over("symbol")
            exprs.append(((pl.col("close") - low_w) / (high_w - low_w)).cast(pl.Float64).alias(f"position_in_range_{w}m"))
            exprs.append((pl.col("close") / high_w - 1.0).cast(pl.Float64).alias(f"dist_from_high_{w}m"))
            exprs.append((pl.col("close") / low_w - 1.0).cast(pl.Float64).alias(f"dist_from_low_{w}m"))
        names = [f"{f}_{w}m" for w in WINDOWS for f in ("position_in_range", "dist_from_high", "dist_from_low")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
