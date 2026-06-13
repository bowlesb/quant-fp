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
from quantlib.features.ols import centered_minutes, ols_window_exprs
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
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
        frame = centered_minutes(frame, "_t")
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            fit = ols_window_exprs("_t", "close", size)
            mean_close = pl.col("close").rolling_mean_by("minute", window_size=size).over("symbol")
            exprs.append((fit["slope"] / mean_close).cast(pl.Float64).alias(f"price_slope_{w}m"))
            exprs.append(fit["r2"].cast(pl.Float64).alias(f"price_r2_{w}m"))
        frame = frame.with_columns(exprs)
        strength = [
            (pl.col(f"price_slope_{w}m") * pl.col(f"price_r2_{w}m")).cast(pl.Float64).alias(f"trend_strength_{w}m")
            for w in WINDOWS
        ]
        frame = frame.with_columns(strength)
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("price_slope", "price_r2", "trend_strength")]
        return frame.select(["symbol", "minute", *names])
