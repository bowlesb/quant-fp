"""Volume features from per-minute bars over windows (family: VOLUME, Layer A)."""
from __future__ import annotations

import datetime as dt

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

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE form: emit ONLY the most recent minute's value per symbol, via a windowed
        aggregate at T (group_by over each window's slice) instead of a rolling pass over the whole
        buffer. ~window× less work per feature; proven byte-identical to compute().filter(T) by
        tests/test_fp_latest.py. The live path uses this; backfill keeps the vectorized rolling form."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume"])
        latest = frame["minute"].max()
        result = frame.filter(pl.col("minute") == latest).select(
            ["symbol", (pl.col("close") * pl.col("volume")).cast(pl.Float64).alias("dollar_volume_1m"),
             pl.col("volume").alias("_volT")]
        )
        for w in WINDOWS:
            low = latest - dt.timedelta(minutes=w)
            agg = (
                frame.filter((pl.col("minute") > low) & (pl.col("minute") <= latest))
                .group_by("symbol")
                .agg([pl.col("volume").mean().alias("_m"), pl.col("volume").std().alias("_s")])
            )
            result = result.join(agg, on="symbol", how="left").with_columns(
                [
                    ((pl.col("_volT") - pl.col("_m")) / pl.col("_s")).cast(pl.Float64).alias(f"volume_zscore_{w}m"),
                    (pl.col("_volT") / pl.col("_m")).cast(pl.Float64).alias(f"volume_ratio_{w}m"),
                ]
            ).drop(["_m", "_s"])
        names = ["dollar_volume_1m"] + [f"volume_zscore_{w}m" for w in WINDOWS] + [f"volume_ratio_{w}m" for w in WINDOWS]
        return result.with_columns(pl.lit(latest).alias("minute")).select(["symbol", "minute", *names])
