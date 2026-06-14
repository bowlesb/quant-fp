"""Volume features from per-minute bars over windows (family: VOLUME, Layer A).

Migrated to the declarative reduction engine: it declares ``reduced``/``points``/``assemble`` ONCE and the
engine generates both the rolling backfill form and the at-T live form (parity by construction). See
quantlib/features/declarative.py.
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

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)


@register
class VolumeGroup(ReductionGroup):
    name = "volume"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)
    windows = WINDOWS

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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...]]]:
        return {"volume": (pl.col("volume"), ("mean", "std"))}

    def points(self) -> dict[str, pl.Expr]:
        return {"volT": pl.col("volume"), "dv": pl.col("close") * pl.col("volume")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {"dollar_volume_1m": pt_("dv")}
        for w in WINDOWS:
            feats[f"volume_zscore_{w}m"] = (pt_("volT") - mean_("volume", w)) / std_("volume", w)
            feats[f"volume_ratio_{w}m"] = pt_("volT") / mean_("volume", w)
        return feats
