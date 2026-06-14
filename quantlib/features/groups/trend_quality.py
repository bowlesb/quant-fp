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
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_, r2_, slope_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
TREND_TOL = 1e-4


@register
class TrendQualityGroup(ReductionGroup):
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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {"close": (pl.col("close"), ("mean",), WINDOWS)}  # mean close normalizes the slope

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        centered_t = (epoch - epoch.min()) / 60.0  # frame-relative time regressor (OLS is origin-invariant)
        return {"trend": (centered_t, pl.col("close"), ("slope", "r2"), WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            price_slope = slope_("trend", w) / mean_("close", w)
            feats[f"price_slope_{w}m"] = price_slope
            feats[f"price_r2_{w}m"] = r2_("trend", w)
            feats[f"trend_strength_{w}m"] = price_slope * r2_("trend", w)
        return feats
