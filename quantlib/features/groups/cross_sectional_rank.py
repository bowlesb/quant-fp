"""Cross-sectional rank features (family: CROSS_SECTIONAL, Layer A).

Where a ticker sits versus the WHOLE universe at the same minute: percentile rank of its trailing
return, its volume, and its dollar volume across all symbols present that minute. These are the
natural inputs to a cross-sectional ranking model (top/bottom deciles).

PARITY NOTE: a rank is only reproducible if the set of symbols ranked is the same live and in
backfill. The values themselves are deterministic; the dependency is universe MEMBERSHIP at each
minute. Today both paths rank over whatever is in minute_agg; the standing follow-up (FEATURE_
TAXONOMY gap #3) is to pin a per-minute universe snapshot so a name missing live but present in
backfill cannot shift everyone's rank. Until then this is coverage-gated like any sparse feature.
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

RETURN_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)


def _percentile_over_minute(value: str) -> pl.Expr:
    """Rank ``value`` within each minute, scaled to [0, 1]; null where fewer than two names present."""
    rank = pl.col(value).rank(method="average").over("minute")
    n = pl.col(value).is_not_null().sum().over("minute")
    return pl.when(n >= 2).then((rank - 1.0) / (n - 1.0)).otherwise(None)


@register
class CrossSectionalRankGroup(FeatureGroup):
    name = "cross_sectional_rank"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in RETURN_WINDOWS:
            specs.append(
                FeatureSpec(name=f"return_rank_{w}m", description=f"Cross-sectional percentile (0-1) of this ticker's trailing {w}-minute return across all symbols present that minute.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
        specs.append(
            FeatureSpec(name="volume_rank_1m", description="Cross-sectional percentile (0-1) of this ticker's last-minute share volume across all symbols present that minute.",
                        dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
        )
        specs.append(
            FeatureSpec(name="dollar_volume_rank_1m", description="Cross-sectional percentile (0-1) of this ticker's last-minute dollar volume (close*volume) across all symbols present that minute.",
                        dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
        )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume"])
        # PARITY PIN (gap #3): if a pinned universe snapshot is provided, rank ONLY within that fixed
        # membership so live and backfill rank the IDENTICAL set (the rank of any symbol depends on the
        # whole set). Without it, the ad-hoc "whoever printed this minute" set can differ across sources
        # and shift everyone's percentile. The universe frame is the same per-day membership both paths
        # load (loaders.load_universe), so the pin is deterministic.
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            frame = frame.join(members, on="symbol", how="inner")
        for w in RETURN_WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
        frame = frame.sort(["symbol", "minute"])
        frame = frame.with_columns(
            [(pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_ret{w}") for w in RETURN_WINDOWS]
            + [(pl.col("close") * pl.col("volume")).alias("_dollar")]
        )
        exprs = [_percentile_over_minute(f"_ret{w}").cast(pl.Float64).alias(f"return_rank_{w}m") for w in RETURN_WINDOWS]
        exprs.append(_percentile_over_minute("volume").cast(pl.Float64).alias("volume_rank_1m"))
        exprs.append(_percentile_over_minute("_dollar").cast(pl.Float64).alias("dollar_volume_rank_1m"))
        names = [f"return_rank_{w}m" for w in RETURN_WINDOWS] + ["volume_rank_1m", "dollar_volume_rank_1m"]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: compute trailing returns from the buffer, then rank ONLY at the latest minute
        (not every minute). Same percentile + universe pin as compute(), parity-guarded."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume"])
        for w in RETURN_WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
        frame = frame.sort(["symbol", "minute"]).with_columns(
            [(pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_ret{w}") for w in RETURN_WINDOWS]
            + [(pl.col("close") * pl.col("volume")).alias("_dollar")]
        )
        latest = frame["minute"].max()
        base = frame.filter(pl.col("minute") == latest)
        if "universe" in ctx.frames:
            base = base.join(ctx.frames["universe"].select("symbol").unique(), on="symbol", how="inner")
        exprs = [_percentile_over_minute(f"_ret{w}").cast(pl.Float64).alias(f"return_rank_{w}m") for w in RETURN_WINDOWS]
        exprs.append(_percentile_over_minute("volume").cast(pl.Float64).alias("volume_rank_1m"))
        exprs.append(_percentile_over_minute("_dollar").cast(pl.Float64).alias("dollar_volume_rank_1m"))
        names = [f"return_rank_{w}m" for w in RETURN_WINDOWS] + ["volume_rank_1m", "dollar_volume_rank_1m"]
        return base.with_columns(exprs).select(["symbol", "minute", *names])
