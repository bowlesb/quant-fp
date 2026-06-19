"""Short-window realized intra-minute range (family: VOLATILITY, Layer A).

WHY (vol-burst finding, experiments/2026-06-19-volburst): a short trailing mean of the intra-minute
high-low range (``rv3`` in the research, the trailing-3-minute mean of ``(high-low)/close``) is one of the
bar-clearing drivers of an imminent large move — in the walk-forward burst classifier (OOS ROC-AUC up to
0.92 for the |forward-return| >= 2% label) ``rv3`` carries real univariate signal alongside the
large-print and inter-arrival burst features. The instantaneous range ``(high-low)/close`` already exists
(``volatility.high_low_range_1m``), but its SHORT trailing mean — the realized-range state that the burst
model actually used — did not. ``parkinson_vol`` is a DIFFERENT estimator (sqrt of mean log(H/L)^2) over
long windows (15-120m); this is the plain trailing mean of the simple range fraction over the short
3/5/10-minute windows the burst study used.

This is a clean ``ReductionGroup`` (mean of a per-bar non-negative ratio) — parity-true by construction
(``compute_latest`` == ``compute``; guarded by tests/test_fp_latest.py). RT-GREEN (windowed mean → O(1)
incremental, ~2.5ms floor tier) and PARITY-GREEN (a bounded windowed reduction). The per-bar value guards
its ``close > 0`` divisor (Guard 2) so a zero/degenerate bar is NULL on both paths.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10)


@register
class RealizedRangeGroup(ReductionGroup):
    name = "realized_range"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"realized_range_{w}m",
                description=(
                    f"Trailing {w}-minute mean of the intra-minute high-low range as a fraction of close "
                    f"((high-low)/close) — short-window realized range (the burst study's rv3). A bar-clearing "
                    f"driver of imminent large moves (vol-burst study, OOS ROC-AUC up to 0.92)."
                ),
                dtype="Float64",
                valid_range=(0.0, 5.0),
                nan_policy="warmup",
                layer="A",
            )
            for w in WINDOWS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # Per-bar realized range fraction; guard the close>0 divisor (Guard 2) so a degenerate zero-price
        # bar contributes NULL (excluded from the window mean) identically on both paths, never inf/nan.
        rng = (
            pl.when(pl.col("close") > 0.0)
            .then((pl.col("high") - pl.col("low")) / pl.col("close"))
            .otherwise(None)
        )
        return {"rng": (rng, ("mean",), WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            value = mean_("rng", w)
            # is_finite() backstop (defense-in-depth): identical on both paths, converts any stray
            # non-finite from the windowed mean into the agreed NULL.
            feats[f"realized_range_{w}m"] = (
                pl.when(value.is_finite()).then(value).otherwise(pl.lit(None, dtype=pl.Float64))
            )
        return feats
