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
from quantlib.features.latest import slice_aggregates
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120, 240)


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

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: trailing high/low per window via one slice+group_by each (aggregate-at-T),
        then derive the same position/distance from the current close. Parity-guarded."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "high", "low"]).sort(["symbol", "minute"])

        def aggs(w: int) -> list[pl.Expr]:
            return [pl.col("high").max().alias(f"_hi_{w}"), pl.col("low").min().alias(f"_lo_{w}")]

        out, latest = slice_aggregates(frame, WINDOWS, aggs)
        close = frame.filter(pl.col("minute") == latest).select(["symbol", pl.col("close").alias("_c")])
        out = out.join(close, on="symbol", how="left")
        exprs = []
        for w in WINDOWS:
            high_w, low_w, close_t = pl.col(f"_hi_{w}"), pl.col(f"_lo_{w}"), pl.col("_c")
            exprs.append(((close_t - low_w) / (high_w - low_w)).cast(pl.Float64).alias(f"position_in_range_{w}m"))
            exprs.append((close_t / high_w - 1.0).cast(pl.Float64).alias(f"dist_from_high_{w}m"))
            exprs.append((close_t / low_w - 1.0).cast(pl.Float64).alias(f"dist_from_low_{w}m"))
        names = [f"{f}_{w}m" for w in WINDOWS for f in ("position_in_range", "dist_from_high", "dist_from_low")]
        return out.with_columns(exprs).with_columns(pl.lit(latest).alias("minute")).select(["symbol", "minute", *names])
