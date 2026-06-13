"""Price-path efficiency features from per-minute close (family: MOMENTUM, Layer A).

Kaufman-style efficiency ratio: net price change over a window divided by the total distance the
price actually travelled (sum of absolute minute steps). Near 1 = a clean directional move; near 0 =
lots of motion that went nowhere (chop). The signed variant keeps the direction. Distinct from trend
R^2 (which fits a line) — efficiency measures path economy, not linearity. Pure rolling sums, so
identical live and backfill.
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

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)


@register
class EfficiencyGroup(FeatureGroup):
    name = "efficiency"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"efficiency_ratio_{w}m", description=f"Kaufman efficiency over {w} minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"directional_efficiency_{w}m", description=f"Signed Kaufman efficiency over {w} minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction).",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="warmup", layer="A")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev1")
        for w in WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
        frame = frame.sort(["symbol", "minute"])
        frame = frame.with_columns((pl.col("close") - pl.col("_prev1")).abs().alias("_step"))
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            path = pl.col("_step").rolling_sum_by("minute", window_size=size).over("symbol")
            net = pl.col("close") - pl.col(f"_lag{w}")
            ratio = pl.when(path > 0.0).then(net / path).otherwise(None)
            exprs.append(ratio.abs().cast(pl.Float64).alias(f"efficiency_ratio_{w}m"))
            exprs.append(ratio.cast(pl.Float64).alias(f"directional_efficiency_{w}m"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("efficiency_ratio", "directional_efficiency")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
