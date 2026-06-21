"""Tradability / borrow flags from the per-symbol reference snapshot (family: REFERENCE, Layer A).

Alpaca asset metadata: is the name shortable, easy to borrow, marginable, fractionable. These are
genuine context for what a strategy can DO with a ticker (a short signal is useless on a hard-to-
borrow name), and unlike sector this data is already populated from Alpaca. Static per symbol, so
identical live and backfill. A NULL flag (symbol absent from asset_metadata) is left NULL, not
coerced — sparse policy.
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

FLAGS: tuple[tuple[str, str], ...] = (
    ("is_shortable", "shortable"),
    ("is_easy_to_borrow", "easy_to_borrow"),
    ("is_marginable", "marginable"),
    ("is_fractionable", "fractionable"),
)


@register
class AssetFlagsGroup(FeatureGroup):
    name = "asset_flags"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.REFERENCE
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
        InputSpec(name="reference", columns=("symbol", "shortable", "easy_to_borrow", "marginable", "fractionable")),
    )

    def declare(self) -> list[FeatureSpec]:
        descriptions = {
            "is_shortable": "1.0 when the symbol can be sold short at the broker, else 0.0 (broadcast across the day).",
            "is_easy_to_borrow": "1.0 when the symbol is on the easy-to-borrow list (cheap, available short locate), else 0.0.",
            "is_marginable": "1.0 when the symbol is marginable (can be held on margin), else 0.0.",
            "is_fractionable": "1.0 when the broker supports fractional-share trading of the symbol, else 0.0.",
        }
        return [
            FeatureSpec(name=feature, description=descriptions[feature], dtype="Float64",
                        valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            for feature, _ in FLAGS
        ]

    def reference_flags(self, ctx: BatchContext) -> pl.DataFrame:
        """The per-symbol reference frame restricted to the raw flag columns the expressions read.
        Shared by compute() and the consolidated point-in-time emit."""
        cols = [column for _, column in FLAGS]
        return ctx.frame("reference").select(["symbol", *cols])

    def exprs(self) -> list[pl.Expr]:
        """The flag-cast feature expressions over the joined raw flag columns (post reference join)."""
        return [pl.col(column).cast(pl.Float64).alias(feature) for feature, column in FLAGS]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"])
        joined = minutes.join(self.reference_flags(ctx), on="symbol", how="left")
        names = [feature for feature, _ in FLAGS]
        return joined.with_columns(self.exprs()).select(["symbol", "minute", *names])

# CI tier-2 proof: harmless comment touching a feature-group file (danger path).
