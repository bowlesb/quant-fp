"""Price-volume interaction features from per-minute bars (family: PRICE_VOLUME, Layer A).

How volume lines up with price direction: where the close sits versus a volume-weighted average,
how much of the window's volume printed on up-bars vs down-bars, and a volume-weighted money-flow
position within each bar. All are ratios of time-anchored rolling sums (no covariance/regression
kernel), so they are numerically stable and identical live vs backfill. Directional cross-correlation
and OBV-slope (which need the windowed-OLS kernel) are intentionally deferred to a later pass.
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

WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120)


@register
class PriceVolumeGroup(FeatureGroup):
    name = "price_volume"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE_VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"vwap_deviation_{w}m", description=f"Close relative to its trailing {w}-minute volume-weighted average price (close/vwap - 1).",
                            dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"up_volume_ratio_{w}m", description=f"Fraction of the trailing {w}-minute share volume that printed on up-bars (positive one-minute return).",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"down_volume_ratio_{w}m", description=f"Fraction of the trailing {w}-minute share volume that printed on down-bars (negative one-minute return).",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"volume_delta_{w}m", description=f"Net directional volume over {w} minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"buying_pressure_{w}m", description=f"Volume-weighted money-flow position over {w} minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "high", "low", "close", "volume"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        rng = pl.col("high") - pl.col("low")
        mfm = pl.when(rng > 0.0).then((2.0 * pl.col("close") - pl.col("high") - pl.col("low")) / rng).otherwise(0.0)
        ret = pl.col("close") / pl.col("_prev") - 1.0
        frame = frame.with_columns(
            [
                (pl.col("close") * pl.col("volume")).alias("_cv"),
                (mfm * pl.col("volume")).alias("_mfv"),
                pl.when(ret > 0.0).then(pl.col("volume")).otherwise(0.0).alias("_up_vol"),
                pl.when(ret < 0.0).then(pl.col("volume")).otherwise(0.0).alias("_dn_vol"),
            ]
        )
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            vol_w = pl.col("volume").rolling_sum_by("minute", window_size=size).over("symbol")
            cv_w = pl.col("_cv").rolling_sum_by("minute", window_size=size).over("symbol")
            mfv_w = pl.col("_mfv").rolling_sum_by("minute", window_size=size).over("symbol")
            up_w = pl.col("_up_vol").rolling_sum_by("minute", window_size=size).over("symbol")
            dn_w = pl.col("_dn_vol").rolling_sum_by("minute", window_size=size).over("symbol")
            exprs.append((pl.col("close") / (cv_w / vol_w) - 1.0).cast(pl.Float64).alias(f"vwap_deviation_{w}m"))
            exprs.append((up_w / vol_w).cast(pl.Float64).alias(f"up_volume_ratio_{w}m"))
            exprs.append((dn_w / vol_w).cast(pl.Float64).alias(f"down_volume_ratio_{w}m"))
            exprs.append(((up_w - dn_w) / vol_w).cast(pl.Float64).alias(f"volume_delta_{w}m"))
            exprs.append((mfv_w / vol_w).cast(pl.Float64).alias(f"buying_pressure_{w}m"))
        names = [
            f"{stat}_{w}m"
            for w in WINDOWS
            for stat in ("vwap_deviation", "up_volume_ratio", "down_volume_ratio", "volume_delta", "buying_pressure")
        ]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
