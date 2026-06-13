"""Price returns over trailing minute windows (family: PRICE).

Returns are point-in-time as of the minute open and span all sessions. The minute grid is complete
(tradeless minutes are present in the substrate), so a positional shift of ``w`` over a
symbol-sorted frame is exactly a ``w``-minute lag.
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

WINDOWS: tuple[int, ...] = (1, 5, 30)


@register
class PriceReturnGroup(FeatureGroup):
    name = "price_returns"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"ret_{w}m",
                description=(
                    f"Simple close-to-close return over the trailing {w} minute(s), point-in-time "
                    f"as of the minute open; spans all sessions."
                ),
                dtype="Float64",
                valid_range=(-1.0, 5.0),
                nan_policy="warmup",
            )
            for w in WINDOWS
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        return frame.with_columns(
            [
                (pl.col("close") / pl.col("close").shift(w).over("symbol") - 1.0)
                .cast(pl.Float64)
                .alias(f"ret_{w}m")
                for w in WINDOWS
            ]
        ).select(["symbol", "minute", *[f"ret_{w}m" for w in WINDOWS]])
