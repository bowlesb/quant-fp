"""Volume features from per-minute bars (family: VOLUME, Layer A)."""
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


@register
class VolumeGroup(FeatureGroup):
    name = "volume"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="dollar_volume_1m",
                description="Dollar volume traded in the last minute (close price * share volume).",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="volume_zscore_30m",
                description="Z-score of the last minute's share volume vs the trailing 30-minute mean and std.",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume"]).sort(["symbol", "minute"])
        # TIME-based rolling so the 30-minute window is wall-clock, correct on gappy grids.
        mean30 = pl.col("volume").rolling_mean_by("minute", window_size="30m").over("symbol")
        std30 = pl.col("volume").rolling_std_by("minute", window_size="30m").over("symbol")
        return frame.with_columns(
            [
                (pl.col("close") * pl.col("volume")).cast(pl.Float64).alias("dollar_volume_1m"),
                ((pl.col("volume") - mean30) / std30).cast(pl.Float64).alias("volume_zscore_30m"),
            ]
        ).select(["symbol", "minute", "dollar_volume_1m", "volume_zscore_30m"])
