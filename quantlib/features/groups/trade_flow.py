"""Trade-flow features from per-minute trade aggregates over windows (family: TRADE_FLOW, Layer B)."""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, pt_, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)


@register
class TradeFlowGroup(ReductionGroup):
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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {
            "sv": (pl.col("signed_volume"), ("sum",), WINDOWS),
            "nt": (pl.col("n_trades"), ("sum",), WINDOWS),
        }

    def points(self) -> dict[str, pl.Expr]:
        return {
            "sv1": pl.col("signed_volume"),
            "nt1": pl.col("n_trades"),
            "accel": (pl.col("n_trades") - pl.col("n_trades").shift(1).over("symbol")) / 60.0,
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {
            "signed_volume_1m": pt_("sv1"),
            "trade_freq_1m": pt_("nt1"),
            "trade_rate_accel_1m": pt_("accel"),
        }
        for w in WINDOWS:
            feats[f"signed_volume_{w}m"] = sum_("sv", w)
            feats[f"trade_freq_{w}m"] = sum_("nt", w)
        return feats
