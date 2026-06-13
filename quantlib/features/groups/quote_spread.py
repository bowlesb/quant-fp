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

QUOTE_WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)


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

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(
            ["symbol", "minute", "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size"]
        ).sort(["symbol", "minute"])
        exprs = [
            pl.col("mean_spread_bps").cast(pl.Float64).alias("spread_bps_1m"),
            pl.col("quote_imbalance").cast(pl.Float64).alias("quote_imbalance_1m"),
            (pl.col("mean_bid_size") + pl.col("mean_ask_size")).cast(pl.Float64).alias("book_depth_1m"),
        ]
        for w in QUOTE_WINDOWS:
            exprs.append(pl.col("mean_spread_bps").rolling_mean_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"spread_bps_{w}m"))
            exprs.append(pl.col("quote_imbalance").rolling_mean_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"quote_imbalance_{w}m"))
        names = ["spread_bps_1m", "quote_imbalance_1m", "book_depth_1m"] + [
            f"{f}_{w}m" for w in QUOTE_WINDOWS for f in ("spread_bps", "quote_imbalance")
        ]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
