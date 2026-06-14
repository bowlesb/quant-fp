"""Quote and spread features from per-minute quote aggregates (family: QUOTE_SPREAD, Layer B)."""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_, pt_
from quantlib.features.registry import register

QUOTE_WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)


@register
class QuoteSpreadGroup(ReductionGroup):
    name = "quote_spread"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.QUOTE_SPREAD
    inputs = (
        InputSpec(
            name="minute_agg",
            columns=("symbol", "minute", "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size"),
        ),
    )

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="spread_bps_1m",
                description="Average top-of-book bid-ask spread in basis points over the last minute.",
                dtype="Float64",
                valid_range=(0.0, 1e5),
                nan_policy="sparse",
                layer="B",
            ),
            FeatureSpec(
                name="quote_imbalance_1m",
                description="Mean top-of-book size imbalance (bid-ask)/(bid+ask) over the last minute.",
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="sparse",
                layer="B",
            ),
            FeatureSpec(
                name="book_depth_1m",
                description="Mean total top-of-book size (bid_size + ask_size) over the last minute.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="B",
            ),
        ]
        for w in QUOTE_WINDOWS:
            specs.append(
                FeatureSpec(name=f"spread_bps_{w}m", description=f"Mean top-of-book spread in basis points over the trailing {w} minutes.",
                            dtype="Float64", valid_range=(0.0, 1e5), nan_policy="sparse", layer="B")
            )
            specs.append(
                FeatureSpec(name=f"quote_imbalance_{w}m", description=f"Mean top-of-book size imbalance over the trailing {w} minutes.",
                            dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="sparse", layer="B")
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {
            "sp": (pl.col("mean_spread_bps"), ("mean",), QUOTE_WINDOWS),
            "qi": (pl.col("quote_imbalance"), ("mean",), QUOTE_WINDOWS),
        }

    def points(self) -> dict[str, pl.Expr]:
        return {
            "sp1": pl.col("mean_spread_bps"),
            "qi1": pl.col("quote_imbalance"),
            "depth": pl.col("mean_bid_size") + pl.col("mean_ask_size"),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {
            "spread_bps_1m": pt_("sp1"),
            "quote_imbalance_1m": pt_("qi1"),
            "book_depth_1m": pt_("depth"),
        }
        for w in QUOTE_WINDOWS:
            feats[f"spread_bps_{w}m"] = mean_("sp", w)
            feats[f"quote_imbalance_{w}m"] = mean_("qi", w)
        return feats
