"""Price-volume interaction features from per-minute bars (family: PRICE_VOLUME, Layer A).

How volume lines up with price: where the close sits versus a volume-weighted average, how much of
the window's volume printed on up- vs down-bars, a volume-weighted money-flow position, the rolling
return/volume correlation, and the slope of on-balance volume. The ratio metrics are time-anchored
rolling sums (stable, no centering); the correlation and OBV-slope use the shared windowed-OLS
kernel (OBV-slope regresses on a centered time axis, so it is origin-invariant and parity-true).
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
from quantlib.features.ols import centered_minutes, with_ols_columns
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)


@register
class PriceVolumeGroup(FeatureGroup):
    name = "price_volume"
    version = "1.1.0"
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
            specs.append(
                FeatureSpec(name=f"pv_correlation_{w}m", description=f"Rolling correlation of one-minute return and share volume over {w} minutes (does volume accompany up or down moves), in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"obv_slope_{w}m", description=f"Slope of on-balance volume regressed on time over {w} minutes, normalized by mean window volume (accumulation/distribution drift).",
                            dtype="Float64", nan_policy="warmup", layer="A", tolerance=1e-4)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "high", "low", "close", "volume"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        frame = centered_minutes(frame, "_t")
        rng = pl.col("high") - pl.col("low")
        mfm = pl.when(rng > 0.0).then((2.0 * pl.col("close") - pl.col("high") - pl.col("low")) / rng).otherwise(0.0)
        ret = pl.col("close") / pl.col("_prev") - 1.0
        signed = pl.when(ret > 0.0).then(pl.col("volume")).when(ret < 0.0).then(-pl.col("volume")).otherwise(0.0)
        frame = frame.with_columns(
            [
                ret.alias("_ret"),
                (pl.col("close") * pl.col("volume")).alias("_cv"),
                (mfm * pl.col("volume")).alias("_mfv"),
                pl.when(ret > 0.0).then(pl.col("volume")).otherwise(0.0).alias("_up_vol"),
                pl.when(ret < 0.0).then(pl.col("volume")).otherwise(0.0).alias("_dn_vol"),
                signed.alias("_signed"),
            ]
        )
        frame = frame.with_columns(pl.col("_signed").cum_sum().over("symbol").alias("_obv"))
        # Materialize each window's rolling sums ONCE (vol_w is shared by 4 ratios), and let
        # with_ols_columns materialize the corr/obv sums once too — polars eager won't CSE them.
        temp = []
        for w in WINDOWS:
            size = f"{w}m"
            temp += [
                pl.col("volume").rolling_sum_by("minute", window_size=size).over("symbol").alias(f"_volw_{w}"),
                pl.col("volume").rolling_mean_by("minute", window_size=size).over("symbol").alias(f"_meanvol_{w}"),
                pl.col("_cv").rolling_sum_by("minute", window_size=size).over("symbol").alias(f"_cvw_{w}"),
                pl.col("_mfv").rolling_sum_by("minute", window_size=size).over("symbol").alias(f"_mfvw_{w}"),
                pl.col("_up_vol").rolling_sum_by("minute", window_size=size).over("symbol").alias(f"_upw_{w}"),
                pl.col("_dn_vol").rolling_sum_by("minute", window_size=size).over("symbol").alias(f"_dnw_{w}"),
            ]
        frame = frame.with_columns(temp)
        for w in WINDOWS:
            size = f"{w}m"
            frame = with_ols_columns(frame, "_ret", "volume", size, {"corr": f"pv_correlation_{w}m"})
            frame = with_ols_columns(frame, "_t", "_obv", size, {"slope": f"_obvslope_{w}"})
        exprs = []
        for w in WINDOWS:
            vol_w = pl.col(f"_volw_{w}")
            exprs.append((pl.col("close") / (pl.col(f"_cvw_{w}") / vol_w) - 1.0).cast(pl.Float64).alias(f"vwap_deviation_{w}m"))
            exprs.append((pl.col(f"_upw_{w}") / vol_w).cast(pl.Float64).alias(f"up_volume_ratio_{w}m"))
            exprs.append((pl.col(f"_dnw_{w}") / vol_w).cast(pl.Float64).alias(f"down_volume_ratio_{w}m"))
            exprs.append(((pl.col(f"_upw_{w}") - pl.col(f"_dnw_{w}")) / vol_w).cast(pl.Float64).alias(f"volume_delta_{w}m"))
            exprs.append((pl.col(f"_mfvw_{w}") / vol_w).cast(pl.Float64).alias(f"buying_pressure_{w}m"))
            exprs.append((pl.col(f"_obvslope_{w}") / pl.col(f"_meanvol_{w}")).cast(pl.Float64).alias(f"obv_slope_{w}m"))
        names = [
            f"{stat}_{w}m"
            for w in WINDOWS
            for stat in ("vwap_deviation", "up_volume_ratio", "down_volume_ratio", "volume_delta", "buying_pressure", "pv_correlation", "obv_slope")
        ]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
