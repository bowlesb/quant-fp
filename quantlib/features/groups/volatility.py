"""Volatility features from per-minute bars over many windows (family: VOLATILITY, Layer A).

Realized vol (std of 1-min returns) and Parkinson vol (from the high-low range) over time-anchored
windows — correct on gappy grids.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_, pt_, std_
from quantlib.features.registry import register

VOL_WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
RANGE_WINDOWS: tuple[int, ...] = (15, 30, 60, 120)
FOUR_LN2 = 2.772588722239781


@register
class VolatilityGroup(ReductionGroup):
    name = "volatility"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="high_low_range_1m",
                description="Intra-minute high-low range as a fraction of close: (high - low) / close.",
                dtype="Float64",
                valid_range=(0.0, 5.0),
                nan_policy="none",
                layer="A",
            )
        ]
        for w in VOL_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"realized_vol_{w}m",
                    description=f"Standard deviation of one-minute close-to-close returns over the trailing {w} minutes.",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=0.02,
                )
            )
        for w in RANGE_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"parkinson_vol_{w}m",
                    description=f"Parkinson high-low volatility estimator over the trailing {w} minutes (uses the bar range).",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=0.02,
                )
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        hl2 = (pl.col("high") / pl.col("low")).log().pow(2)
        return {"ret": (ret, ("std",), VOL_WINDOWS), "hl2": (hl2, ("mean",), RANGE_WINDOWS)}

    def points(self) -> dict[str, pl.Expr]:
        return {"hlr": (pl.col("high") - pl.col("low")) / pl.col("close")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {"high_low_range_1m": pt_("hlr")}
        for w in VOL_WINDOWS:
            feats[f"realized_vol_{w}m"] = std_("ret", w)
        for w in RANGE_WINDOWS:
            # mean(hl2) is mathematically NON-NEGATIVE (hl2 = log(high/low)^2 >= 0), but the live incremental
            # running sum of a window of all-flat (high==low -> hl2==0) bars can drift to a tiny NEGATIVE
            # residue (~-1e-22) from the add/expire float cycle, whose sqrt is NaN — while the backfill
            # rolling_mean returns exactly 0.0 (a live-vs-backfill parity break on sparse/flat symbols). Clip
            # the non-negative quantity to >=0 before the sqrt, exactly as ohlc_vol's garman_klass/
            # rogers_satchell already do for the identical drift.
            feats[f"parkinson_vol_{w}m"] = (mean_("hl2", w) / FOUR_LN2).clip(lower_bound=0.0).sqrt()
        return feats
