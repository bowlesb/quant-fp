"""Sector one-hot features from the per-symbol reference snapshot (family: REFERENCE, Layer A).

Eleven GICS-aligned sector buckets (FMP labels) plus an explicit unknown bucket, one-hot encoded and
broadcast to every minute of the symbol. The sector map is static and source-independent, so these
are identical live and in backfill — parity-true by construction. Until the FMP key is wired the
sector column is NULL for all symbols, so every name lands in sector_is_unknown; the encoding is
correct the moment the map populates, no feature change needed.
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

# Canonical buckets = FMP GICS-aligned labels normalized to lower_snake_case.
SECTORS: tuple[str, ...] = (
    "technology",
    "healthcare",
    "financial_services",
    "consumer_cyclical",
    "consumer_defensive",
    "industrials",
    "energy",
    "basic_materials",
    "real_estate",
    "utilities",
    "communication_services",
)


@register
class SectorOneHotGroup(FeatureGroup):
    name = "sector"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.REFERENCE
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
        InputSpec(name="reference", columns=("symbol", "sector")),
    )

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(name=f"sector_is_{sector}", description=f"1.0 when the symbol's GICS-aligned sector is {sector.replace('_', ' ')}, else 0.0 (one-hot, broadcast across the day).",
                        dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="none", layer="A")
            for sector in SECTORS
        ]
        specs.append(
            FeatureSpec(name="sector_is_unknown", description="1.0 when the symbol has no mapped sector (unlisted in the sector map or FMP could not classify it), else 0.0.",
                        dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="none", layer="A")
        )
        return specs

    def reference_norm(self, ctx: BatchContext) -> pl.DataFrame:
        """The per-symbol reference frame with the normalized ``_norm`` sector column the feature
        expressions read. Shared by compute() and the consolidated point-in-time emit."""
        return ctx.frame("reference").select(["symbol", "sector"]).with_columns(
            pl.col("sector").str.to_lowercase().str.replace_all(" ", "_").alias("_norm")
        )

    def exprs(self) -> list[pl.Expr]:
        """The one-hot feature expressions over the joined ``_norm`` column (post reference join)."""
        exprs = [
            (pl.col("_norm") == sector).fill_null(False).cast(pl.Float64).alias(f"sector_is_{sector}")
            for sector in SECTORS
        ]
        exprs.append(
            pl.when(pl.col("_norm").is_in(SECTORS)).then(0.0).otherwise(1.0).cast(pl.Float64).alias("sector_is_unknown")
        )
        return exprs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"])
        joined = minutes.join(self.reference_norm(ctx), on="symbol", how="left")
        names = [spec.name for spec in self.declare()]
        return joined.with_columns(self.exprs()).select(["symbol", "minute", *names])
