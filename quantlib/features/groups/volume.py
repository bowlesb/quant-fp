"""Volume features from per-minute bars over windows (family: VOLUME, Layer A)."""
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

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)


@register
class VolumeGroup(FeatureGroup):
    name = "volume"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="dollar_volume_1m",
                description="Dollar volume traded in the last minute (close price * share volume).",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="none",
                layer="A",
            )
        ]
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"volume_zscore_{w}m",
                    description=f"Z-score of the last minute's share volume vs the trailing {w}-minute mean and std.",
                    dtype="Float64",
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"volume_ratio_{w}m",
                    description=f"Ratio of the last minute's share volume to its trailing {w}-minute mean.",
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume"]).sort(["symbol", "minute"])
        exprs = [(pl.col("close") * pl.col("volume")).cast(pl.Float64).alias("dollar_volume_1m")]
        for w in WINDOWS:
            mean_w = pl.col("volume").rolling_mean_by("minute", window_size=f"{w}m").over("symbol")
            std_w = pl.col("volume").rolling_std_by("minute", window_size=f"{w}m").over("symbol")
            exprs.append(((pl.col("volume") - mean_w) / std_w).cast(pl.Float64).alias(f"volume_zscore_{w}m"))
            exprs.append((pl.col("volume") / mean_w).cast(pl.Float64).alias(f"volume_ratio_{w}m"))
        names = ["dollar_volume_1m"] + [f"volume_zscore_{w}m" for w in WINDOWS] + [f"volume_ratio_{w}m" for w in WINDOWS]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
