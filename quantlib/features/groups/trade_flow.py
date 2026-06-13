"""Trade-flow features from per-minute trade aggregates (family: TRADE_FLOW).

These summarize the last minute's trading: signed pressure, trade frequency, and the change in
trade rate (a coarse "is this name starting to take off" proxy from minute aggregates — the
sub-second burst version lands once raw-tick capture is wired).
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


@register
class TradeFlowGroup(FeatureGroup):
    name = "trade_flow"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "n_trades", "signed_volume")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="signed_volume_1m",
                description="Buy-minus-sell signed share volume over the last minute (tick-rule signed).",
                dtype="Float64",
                nan_policy="none",
                # 1% relative tolerance (vs the 1e-6 default): signed volume sums hundreds of
                # provisional trades, so the live firehose and the settled tape rarely agree to the
                # share. Justified — counts match 99.5%, net sign is 99.84% stable, the settled
                # backfill is training truth; see docs/LIFECYCLE_DEMOS.md. Heavy tail = large
                # closing-auction blocks (candidate for a more robust variant later).
                tolerance=0.01,
            ),
            FeatureSpec(
                name="trade_freq_1m",
                description="Number of trades printed in the last minute (raw trade frequency).",
                dtype="Float64",
                valid_range=(0.0, 1e7),
                nan_policy="none",
            ),
            FeatureSpec(
                name="trade_rate_accel_1m",
                description="Change in trades-per-second versus the prior minute (trade-rate acceleration).",
                dtype="Float64",
                nan_policy="warmup",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "n_trades", "signed_volume"])
        frame = lagged(frame, "n_trades", 1, "_n_trades_prev")
        return frame.with_columns(
            [
                pl.col("signed_volume").cast(pl.Float64).alias("signed_volume_1m"),
                pl.col("n_trades").cast(pl.Float64).alias("trade_freq_1m"),
                ((pl.col("n_trades") - pl.col("_n_trades_prev")).cast(pl.Float64) / 60.0).alias(
                    "trade_rate_accel_1m"
                ),
            ]
        ).select(["symbol", "minute", "signed_volume_1m", "trade_freq_1m", "trade_rate_accel_1m"])
