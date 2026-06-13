"""Volatility features from per-minute bars (family: VOLATILITY, Layer A)."""
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

VOL_WINDOW = 5


@register
class VolatilityGroup(FeatureGroup):
    name = "volatility"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="high_low_range_1m",
                description="Intra-minute high-low range as a fraction of close: (high - low) / close.",
                dtype="Float64",
                valid_range=(0.0, 5.0),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="realized_vol_5m",
                description="Standard deviation of the last 5 one-minute close-to-close returns (realized vol).",
                dtype="Float64",
                valid_range=(0.0, 5.0),
                nan_policy="warmup",
                layer="A",
                # 2% relative tolerance: a 2nd-order windowed stat amplifies thin-tier bar-close
                # diffs. 90% of Tier-3 cells are exact; 2% lifts T3 to 96.4%. See LIFECYCLE_DEMOS.
                tolerance=0.02,
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "high", "low", "close"]).sort(
            ["symbol", "minute"]
        )
        frame = lagged(frame, "close", 1, "_close_prev")
        frame = frame.with_columns((pl.col("close") / pl.col("_close_prev") - 1.0).alias("_ret_1m"))
        return frame.with_columns(
            [
                ((pl.col("high") - pl.col("low")) / pl.col("close")).cast(pl.Float64).alias("high_low_range_1m"),
                pl.col("_ret_1m").rolling_std(window_size=VOL_WINDOW).over("symbol").cast(pl.Float64).alias(
                    "realized_vol_5m"
                ),
            ]
        ).select(["symbol", "minute", "high_low_range_1m", "realized_vol_5m"])
