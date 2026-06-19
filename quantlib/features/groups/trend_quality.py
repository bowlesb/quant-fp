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
from quantlib.features.declarative import ReductionGroup, StatefulRegressor, mean_, r2_, slope_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
TREND_TOL = 1e-4


@register
class TrendQualityGroup(ReductionGroup):
    name = "trend_quality"
    # 1.1.0: n==2 perfect-fit guard makes price_r2 exactly 1.0 at the b==2 corner (was ~0.9998 float noise).
    version = "1.1.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # price_r2 is the OLS R² = cov²/(var_x·var_y) of close on time. The two former incremental-vs-batch breaches
    # are now closed AT SOURCE: (1) the rolling time-OLS origin-rebase (PR #132) keeps x small so the variance
    # term is well conditioned for every n>=3 cell, and (2) the n==2 perfect-fit guard (_OLS_PERFECT_FIT_COUNT)
    # emits the EXACT r2=1.0 at the b==2 corner where cov²/(var_x·var_y) was noise/noise. With both, batch and
    # incremental agree cell-for-cell on smooth/degenerate/n==2 walks, so the fast path is parity-true here. The
    # n==2 guard changes the degenerate-cell value (0.9998->1.0) -> the version bump above.
    incremental_safe = True

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

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        return {"trend": [StatefulRegressor(slot="x", kind="time")]}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            slope = slope_("trend", w)
            raw_r2 = r2_("trend", w)
            # Flat price over a *warmed* window: the slope is defined (≈0) but the R^2 denominator (price
            # variance) is zero, so the shared OLS kernel leaves R^2 undefined — null on the polars/backfill
            # path, NaN on the numpy/live path. A flat line has zero *explained* variance (not an undefined
            # fit), so pin R^2=0 there. This keeps trend_strength = slope·R^2 = 0 (a real, valid 0-trend)
            # instead of silently nulling a row whose slope IS defined, and — because the guard is applied
            # here in the shared assemble() and treats null↔NaN identically — backfill and live agree by
            # construction (parity). True warmup (slope still missing) is untouched and stays missing.
            slope_defined = slope.is_not_null() & slope.is_not_nan()
            r2_undefined = raw_r2.is_null() | raw_r2.is_nan()
            r2 = pl.when(slope_defined & r2_undefined).then(0.0).otherwise(raw_r2)
            price_slope = slope / mean_("close", w)
            feats[f"price_slope_{w}m"] = price_slope
            feats[f"price_r2_{w}m"] = r2
            feats[f"trend_strength_{w}m"] = price_slope * r2
        return feats
