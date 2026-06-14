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
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, pt_, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)


@register
class EfficiencyGroup(ReductionGroup):
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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        step = (pl.col("close") - pl.col("close").shift(1).over("symbol")).abs()  # |minute-to-minute move|
        return {"step": (step, ("sum",), WINDOWS)}

    def points(self) -> dict[str, pl.Expr]:
        pts: dict[str, pl.Expr] = {"c": pl.col("close")}
        for w in WINDOWS:
            pts[f"l{w}"] = pl.col("close").shift(w).over("symbol")
        return pts

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            path = sum_("step", w)
            ratio = pl.when(path > 0.0).then((pt_("c") - pt_(f"l{w}")) / path).otherwise(None)
            feats[f"efficiency_ratio_{w}m"] = ratio.abs()
            feats[f"directional_efficiency_{w}m"] = ratio
        return feats
