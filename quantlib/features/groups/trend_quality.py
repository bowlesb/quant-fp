"""Trend-quality features: how cleanly price is trending (family: TREND_QUALITY, Layer A).

A trailing ordinary-least-squares fit of close on time over each window, expressed via rolling sums
so it is a single vectorized pass. We measure the slope (normalized to a fractional move per minute),
the fit's R-squared (how linear the move is), and a signed quality-weighted strength (slope * R^2).

Numerical note (parity): the time regressor ``x`` is centered on the frame's earliest minute so its
magnitudes stay small and the variance terms n*Sxx - Sx^2 are well conditioned. OLS slope is
invariant to the choice of x-origin, so the live trailing buffer and the settled backfill (different
earliest minutes) agree to floating-point precision. A modest 1e-4 tolerance absorbs the residual.
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

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
TREND_TOL = 1e-4


@register
class TrendQualityGroup(FeatureGroup):
    name = "trend_quality"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"price_slope_{w}m", description=f"OLS slope of close on time over the trailing {w} minutes, normalized as a fractional price move per minute.",
                            dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="warmup", layer="A", tolerance=TREND_TOL)
            )
            specs.append(
                FeatureSpec(name=f"price_r2_{w}m", description=f"R-squared of the trailing {w}-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A", tolerance=TREND_TOL)
            )
            specs.append(
                FeatureSpec(name=f"trend_strength_{w}m", description=f"Signed quality-weighted trend over {w} minutes: normalized slope times R-squared (steep AND clean moves score highest).",
                            dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="warmup", layer="A", tolerance=TREND_TOL)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        x0 = frame.select(pl.col("minute").dt.epoch("s").min()).item()
        frame = frame.with_columns(
            [
                ((pl.col("minute").dt.epoch("s").cast(pl.Float64) - float(x0)) / 60.0).alias("_x"),
                pl.col("close").cast(pl.Float64).alias("_y"),
                pl.lit(1.0).alias("_one"),
            ]
        )
        frame = frame.with_columns(
            [
                (pl.col("_x") * pl.col("_y")).alias("_xy"),
                (pl.col("_x") * pl.col("_x")).alias("_xx"),
                (pl.col("_y") * pl.col("_y")).alias("_yy"),
            ]
        )
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            n = pl.col("_one").rolling_sum_by("minute", window_size=size).over("symbol")
            sx = pl.col("_x").rolling_sum_by("minute", window_size=size).over("symbol")
            sy = pl.col("_y").rolling_sum_by("minute", window_size=size).over("symbol")
            sxy = pl.col("_xy").rolling_sum_by("minute", window_size=size).over("symbol")
            sxx = pl.col("_xx").rolling_sum_by("minute", window_size=size).over("symbol")
            syy = pl.col("_yy").rolling_sum_by("minute", window_size=size).over("symbol")
            denom_x = n * sxx - sx * sx
            denom_y = n * syy - sy * sy
            cov_n = n * sxy - sx * sy
            mean_y = sy / n
            slope = cov_n / denom_x
            slope_norm = slope / mean_y
            r2 = (cov_n * cov_n) / (denom_x * denom_y)
            defined = (n >= 2.0) & (denom_x > 0.0)
            slope_norm_e = pl.when(defined).then(slope_norm).otherwise(None).cast(pl.Float64)
            r2_e = pl.when(defined & (denom_y > 0.0)).then(r2).otherwise(None).cast(pl.Float64)
            exprs.append(slope_norm_e.alias(f"price_slope_{w}m"))
            exprs.append(r2_e.alias(f"price_r2_{w}m"))
            exprs.append((slope_norm_e * r2_e).alias(f"trend_strength_{w}m"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("price_slope", "price_r2", "trend_strength")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
