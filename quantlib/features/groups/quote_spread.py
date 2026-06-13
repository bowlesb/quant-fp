"""Quote and spread features from per-minute quote aggregates (family: QUOTE_SPREAD, Layer B)."""
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
class QuoteSpreadGroup(FeatureGroup):
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
        return [
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

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(
            ["symbol", "minute", "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size"]
        )
        return frame.with_columns(
            [
                pl.col("mean_spread_bps").cast(pl.Float64).alias("spread_bps_1m"),
                pl.col("quote_imbalance").cast(pl.Float64).alias("quote_imbalance_1m"),
                (pl.col("mean_bid_size") + pl.col("mean_ask_size")).cast(pl.Float64).alias("book_depth_1m"),
            ]
        ).select(["symbol", "minute", "spread_bps_1m", "quote_imbalance_1m", "book_depth_1m"])
