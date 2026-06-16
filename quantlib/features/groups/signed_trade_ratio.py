"""Normalized signed-trade imbalance — net signed volume as a fraction of total volume (family:
TRADE_FLOW, Layer B).

The existing ``trade_flow`` group exposes ``signed_volume_{w}m`` in SHARES, which is dominated by high-ADV
names and so is not comparable in a cross-section. This group normalizes it by total volume over the same
window to a scale-free ``signed_trade_ratio_{w}m`` in [-1, 1] — the standard microstructure "trade-flow
imbalance" and the natural cross-sectional ranker. It is a Case-A feature (one file over the EXISTING
``minute_agg`` columns ``signed_volume`` + ``volume`` — no aggregates/loaders change) and, being a
``ReductionGroup``, is parity-true by construction (``compute_latest == compute().filter(last minute)``,
auto-guarded by ``tests/test_fp_latest.py``).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 15, 30, 60)


@register
class SignedTradeRatioGroup(ReductionGroup):
    name = "signed_trade_ratio"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "signed_volume", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"signed_trade_ratio_{w}m",
                description=(
                    f"Net signed (buy-minus-sell, tick-rule) share volume as a fraction of total share "
                    f"volume over the trailing {w} minutes — scale-free trade-flow imbalance in [-1, 1]."
                ),
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="warmup",
                layer="B",
            )
            for w in WINDOWS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {
            "sv": (pl.col("signed_volume"), ("sum",), WINDOWS),
            "vol": (pl.col("volume"), ("sum",), WINDOWS),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        # Zero/null-volume windows -> null (the ratio is mathematically undefined), never 0 — let the
        # absence of trading show rather than fabricate a balanced book.
        return {
            f"signed_trade_ratio_{w}m": pl.when(sum_("vol", w) > 0)
            .then(sum_("sv", w) / sum_("vol", w))
            .otherwise(None)
            for w in WINDOWS
        }
