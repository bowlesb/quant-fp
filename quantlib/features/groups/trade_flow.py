"""Trade-flow features from per-minute trade aggregates over windows (family: TRADE_FLOW, Layer B)."""
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

WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60)


@register
class TradeFlowGroup(FeatureGroup):
    name = "trade_flow"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "n_trades", "signed_volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="signed_volume_1m",
                description="Buy-minus-sell signed share volume over the last minute (tick-rule signed).",
                dtype="Float64",
                layer="B",
                tolerance=0.01,
            ),
            FeatureSpec(
                name="trade_freq_1m",
                description="Number of trades printed in the last minute (raw trade frequency).",
                dtype="Float64",
                valid_range=(0.0, 1e7),
                layer="B",
            ),
            FeatureSpec(
                name="trade_rate_accel_1m",
                description="Change in trades-per-second versus the prior minute (trade-rate acceleration).",
                dtype="Float64",
                nan_policy="warmup",
                layer="B",
            ),
        ]
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"signed_volume_{w}m",
                    description=f"Sum of signed share volume over the trailing {w} minutes (net buy/sell pressure).",
                    dtype="Float64",
                    nan_policy="warmup",
                    layer="B",
                    tolerance=0.01,
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"trade_freq_{w}m",
                    description=f"Total number of trades over the trailing {w} minutes.",
                    dtype="Float64",
                    valid_range=(0.0, 1e9),
                    nan_policy="warmup",
                    layer="B",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "n_trades", "signed_volume"])
        frame = lagged(frame, "n_trades", 1, "_n_prev").sort(["symbol", "minute"])
        exprs = [
            pl.col("signed_volume").cast(pl.Float64).alias("signed_volume_1m"),
            pl.col("n_trades").cast(pl.Float64).alias("trade_freq_1m"),
            ((pl.col("n_trades") - pl.col("_n_prev")).cast(pl.Float64) / 60.0).alias("trade_rate_accel_1m"),
        ]
        for w in WINDOWS:
            exprs.append(pl.col("signed_volume").rolling_sum_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"signed_volume_{w}m"))
            exprs.append(pl.col("n_trades").rolling_sum_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"trade_freq_{w}m"))
        names = ["signed_volume_1m", "trade_freq_1m", "trade_rate_accel_1m"] + [
            f"{f}_{w}m" for w in WINDOWS for f in ("signed_volume", "trade_freq")
        ]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
