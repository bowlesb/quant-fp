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
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, StatefulRegressor, corr_, mean_, pt_, slope_, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)


@register
class PriceVolumeGroup(ReductionGroup):
    name = "price_volume"
    # 1.2.0: n==2 perfect-fit guard makes pv_correlation exactly sign(cov) at the b==2 corner.
    version = "1.2.0"
    owner = "modeller"
    type = FeatureType.PRICE_VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close", "volume")),)
    # pv_correlation is a Pearson corr (cov / √(var_x·var_y)) of one-minute return against RAW share volume.
    # The n==2 perfect-fit guard (_OLS_PERFECT_FIT_COUNT, #155) closes ONLY the b==2 perfect-fit corner — it does
    # NOT cover the general gappy-window case: when a sparse symbol skips minutes the in-window volume regressor
    # x≈0 over the window, so the corr denominator denom_x = b·Σx²−(Σx)² is a difference of float-noise that
    # incremental's running Σx² rounds differently from the batch fresh sum, straddling the defined-guard at
    # n>2 cells — incremental emits where batch NULLs. Same conditioning class as `volume`; route LIVE to the
    # batch fresh-sum recompute. (OBV-slope regresses on a centered time axis and is well-conditioned; it rides
    # the batch path here with no loss. The shared centered-denom kernel is the queued follow-up to widen this.)
    incremental_safe = False

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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        rng = pl.col("high") - pl.col("low")
        mfm = pl.when(rng > 0.0).then((2.0 * pl.col("close") - pl.col("high") - pl.col("low")) / rng).otherwise(0.0)
        vol = pl.col("volume")
        return {
            "vol": (vol, ("sum", "mean"), WINDOWS),  # sum feeds the 4 ratios; mean normalizes obv_slope
            "cv": (pl.col("close") * vol, ("sum",), WINDOWS),
            "mfv": (mfm * vol, ("sum",), WINDOWS),
            "up": (pl.when(ret > 0.0).then(vol).otherwise(0.0), ("sum",), WINDOWS),
            "dn": (pl.when(ret < 0.0).then(vol).otherwise(0.0), ("sum",), WINDOWS),
        }

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        signed = pl.when(ret > 0.0).then(pl.col("volume")).when(ret < 0.0).then(-pl.col("volume")).otherwise(0.0)
        obv = signed.cum_sum().over("symbol")
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        centered_t = (epoch - epoch.min()) / 60.0  # frame-relative time regressor (OLS is origin-invariant)
        return {
            "pv": (ret, pl.col("volume"), ("corr",), WINDOWS),  # return-vs-volume correlation
            "obv": (centered_t, obv, ("slope",), WINDOWS),  # on-balance-volume slope on time
        }

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        signed = pl.when(ret > 0.0).then(pl.col("volume")).when(ret < 0.0).then(-pl.col("volume")).otherwise(0.0)
        return {
            "obv": [
                StatefulRegressor(slot="x", kind="time"),
                StatefulRegressor(slot="y", kind="cumulative", increment=signed),
            ]
        }

    def points(self) -> dict[str, pl.Expr]:
        return {"cT": pl.col("close")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            vol_w = sum_("vol", w)
            feats[f"vwap_deviation_{w}m"] = pt_("cT") / (sum_("cv", w) / vol_w) - 1.0
            feats[f"up_volume_ratio_{w}m"] = sum_("up", w) / vol_w
            feats[f"down_volume_ratio_{w}m"] = sum_("dn", w) / vol_w
            feats[f"volume_delta_{w}m"] = (sum_("up", w) - sum_("dn", w)) / vol_w
            feats[f"buying_pressure_{w}m"] = sum_("mfv", w) / vol_w
            feats[f"pv_correlation_{w}m"] = corr_("pv", w)
            feats[f"obv_slope_{w}m"] = slope_("obv", w) / mean_("vol", w)
        return feats
