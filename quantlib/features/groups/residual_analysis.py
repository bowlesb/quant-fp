"""Residual-analysis features: the distribution of close around its linear trend (family: TREND_QUALITY, Layer A).

These extend ``trend_quality`` (the OLS slope/R^2 exemplar) from "is price trending" to "is the move CLEAN":
small, symmetric residuals around the fitted line are a tradeable straight-line move; large residuals are chop
dressed up as a trend. The fit is the SAME trailing OLS of close on a frame-relative time axis ``trend_quality``
uses (residuals are origin-invariant).

These are POWER-SUM moments of the OLS residuals, computed VECTORIZED via polars ``rolling_sum_by`` over the
raw power-sum columns (1, x, y, x^2, x*y, y^2) — one pass, no per-row Python and no second formulation. The
residual sum-of-squares is the closed form ``Σr^2 = Syy_c - slope·Sxy_c`` of those sums, so ``residual_std`` =
``sqrt(Σr^2/n) / mean_close · 100``. Centering the time axis on the frame's earliest minute keeps the sums
well-conditioned (origin-invariant residuals), so the difference-of-sums stays accurate even for a near-perfect
fit. ``residual_mean_abs`` is the absolute mean residual (~0 for an OLS fit — the honest near-zero baseline),
and ``residuals_symmetric`` is the old codebase's clean-fit flag (near-zero mean residual).

Why a hand-written ``FeatureGroup`` and not a ``ReductionGroup``: the residual SS of a near-perfect intraday
fit is the catastrophic-cancellation difference of two nearly-equal sums, so it must be evaluated from ONE sum
source. ``compute_latest`` is the base-class default (the SAME rolling pass filtered to T), so backfill and
live ride the IDENTICAL polars expression — no kernel-vs-rolling float divergence to fail parity (guarded by
tests/test_fp_latest.py). residual SKEW (the third moment) lives in ``momentum_run`` alongside the longest-run.
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
RESID_TOL = 1e-4

# Old-codebase clean-fit threshold: residuals (percent of mean price) are "symmetric" when the absolute mean
# residual is under 0.1 percent. The OLS mean residual is ~0, so this is effectively always satisfied for a
# warm window; kept so the flag's semantics match the original feature.
SYMMETRIC_MEAN_PCT = 0.1
MIN_POINTS = 4.0  # the old codebase required >=4 closes for a meaningful residual distribution


def _residual_columns(w: int) -> dict[str, pl.Expr]:
    """The OLS residual std/mean-abs columns over the trailing ``w`` minutes, from the rolling power sums of
    the centered time axis (__x) and close. Undefined cells (n < MIN_POINTS or zero x-variance) -> null."""
    size = f"{w}m"

    def roll(name: str) -> pl.Expr:
        return pl.col(name).rolling_sum_by("minute", window_size=size).over("symbol")

    n = roll("__one")
    sx, sy = roll("__x"), pl.col("close").rolling_sum_by("minute", window_size=size).over("symbol")
    sxx, sxy, syy = roll("__xx"), roll("__xy"), roll("__yy")
    sxx_c = sxx - sx * sx / n
    sxy_c = sxy - sx * sy / n
    syy_c = syy - sy * sy / n
    slope = sxy_c / sxx_c
    ssr = (syy_c - slope * sxy_c).clip(lower_bound=0.0)
    mean_close = sy / n
    defined = (n >= MIN_POINTS) & (sxx_c > 0.0)
    resid_std = pl.when(defined).then((ssr / n).sqrt() / mean_close * 100.0).otherwise(None)
    # The OLS mean residual is identically 0; carry the definedness so it warms up with resid_std.
    mean_abs = pl.when(defined).then(0.0).otherwise(None)
    return {"std": resid_std, "mean_abs": mean_abs}


@register
class ResidualAnalysisGroup(FeatureGroup):
    name = "residual_analysis"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"residual_std_{w}m", description=f"Std of OLS residuals around the {w}-minute close-vs-time trend, as a percent of mean price: how tightly price hugs its trend line.",
                            dtype="Float64", valid_range=(0.0, 100.0), nan_policy="warmup", layer="A", tolerance=RESID_TOL)
            )
            specs.append(
                FeatureSpec(name=f"residual_mean_abs_{w}m", description=f"Absolute mean OLS residual over {w} minutes (percent of mean price); ~0 for a clean fit — the kept-honest baseline of the residual distribution.",
                            dtype="Float64", valid_range=(0.0, 100.0), nan_policy="warmup", layer="A", tolerance=RESID_TOL)
            )
            specs.append(
                FeatureSpec(name=f"residuals_symmetric_{w}m", description=f"1.0 when the {w}-minute trend residuals are near-symmetric (absolute mean residual under 0.1 percent of price), else 0.0 — a clean-fit flag.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A", tolerance=RESID_TOL, storage="UInt8")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        if frame.height == 0:
            schema = {"symbol": pl.String, "minute": pl.Datetime("us", "UTC"), **{name: pl.Float64 for name in self.feature_names}}
            return pl.DataFrame(schema=schema)
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        # frame-relative time axis (origin-invariant residuals) keeps the power sums well-conditioned.
        frame = frame.with_columns(((epoch - epoch.min()) / 60.0).alias("__x"), pl.lit(1.0).alias("__one"))
        frame = frame.with_columns(
            (pl.col("__x") * pl.col("__x")).alias("__xx"),
            (pl.col("__x") * pl.col("close")).alias("__xy"),
            (pl.col("close") * pl.col("close")).alias("__yy"),
        )
        feats: list[pl.Expr] = []
        for w in WINDOWS:
            cols = _residual_columns(w)
            resid_std = cols["std"]
            mean_abs = cols["mean_abs"]
            feats.append(resid_std.alias(f"residual_std_{w}m"))
            feats.append(mean_abs.alias(f"residual_mean_abs_{w}m"))
            symmetric = pl.when(mean_abs.is_null()).then(None).when(mean_abs < SYMMETRIC_MEAN_PCT).then(1.0).otherwise(0.0)
            feats.append(symmetric.alias(f"residuals_symmetric_{w}m"))
        return frame.with_columns(feats).select(["symbol", "minute", *self.feature_names])
