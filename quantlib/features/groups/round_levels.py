"""Round-number proximity features from per-minute close (family: PRICE, Layer A).

Price clusters and reacts at round numbers (whole and half dollars) — magnets for orders and stops.
Distance to the nearest whole/half dollar and a near-round flag. Pure function of the close, no
lookback, so buffer-independent and trivially parity-true.
"""
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

NEAR_DOLLAR_THRESHOLD = 0.02  # within 2 cents of a whole dollar


@register
class RoundLevelsGroup(FeatureGroup):
    name = "round_levels"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="dist_to_round_dollar",
                description="Absolute distance from the close to the nearest whole dollar, in dollars (0 to 0.5).",
                dtype="Float64",
                valid_range=(0.0, 0.51),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="dist_to_half_dollar",
                description="Absolute distance from the close to the nearest half dollar (x.00 or x.50), in dollars (0 to 0.25).",
                dtype="Float64",
                valid_range=(0.0, 0.26),
                nan_policy="none",
                layer="A",
            ),
            FeatureSpec(
                name="is_at_round_dollar",
                description="1.0 when the close is within 2 cents of a whole dollar, else 0.0 (round-number cluster).",
                dtype="Float64",
                valid_range=(-0.01, 1.01),
                nan_policy="none",
                layer="A",
            ),
        ]

    def exprs(self) -> list[pl.Expr]:
        """The feature column expressions (functions of ``close``), shared by compute() and the
        consolidated point-in-time emit."""
        close = pl.col("close")
        frac_dollar = close - close.floor()
        dist_dollar = pl.min_horizontal(frac_dollar, 1.0 - frac_dollar)
        half = close * 2.0
        frac_half = (half - half.floor()) / 2.0
        dist_half = pl.min_horizontal(frac_half, 0.5 - frac_half)
        return [
            dist_dollar.cast(pl.Float64).alias("dist_to_round_dollar"),
            dist_half.cast(pl.Float64).alias("dist_to_half_dollar"),
            (dist_dollar < NEAR_DOLLAR_THRESHOLD).cast(pl.Float64).alias("is_at_round_dollar"),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return (
            ctx.frame("minute_agg")
            .select(["symbol", "minute", "close"])
            .with_columns(self.exprs())
            .select(
                ["symbol", "minute", "dist_to_round_dollar", "dist_to_half_dollar", "is_at_round_dollar"]
            )
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        # Per-row function of the minute's close (round-number distances) — no cross-minute window, so compute
        # ONLY the latest minute instead of the whole buffer. Parity-guarded by test_fp_latest.
        return self.compute_latest_point_in_time(ctx)
