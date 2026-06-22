"""Return-dynamics features from per-minute close (family: MOMENTUM, Layer A).

The temporal STRUCTURE of returns, beyond their level: lag-1 / lag-2 autocorrelation (a mean-
reversion vs momentum signature) via the shared OLS kernel, and return acceleration (is the trailing
move speeding up or fading). Pure functions of close on a time-anchored grid -> identical live and
backfill; autocorrelation regresses returns on their own lag, both small-magnitude, so no centering
is needed.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, corr_, pt_
from quantlib.features.registry import register

AUTOCORR_WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
ACCEL_WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60)
ACCEL_LAGS: tuple[int, ...] = tuple(sorted({w for w in ACCEL_WINDOWS} | {2 * w for w in ACCEL_WINDOWS}))
DYN_TOL = 1e-4


@register
class ReturnDynamicsGroup(ReductionGroup):
    name = "return_dynamics"
    version = "1.1.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # The autocorrelation OLS regressor x is the LAGGED one-minute return; the regressand y is the return — BOTH
    # near-zero on a gappy near-constant window, so denom_x = b·Σx²−(Σx)² AND denom_y are catastrophic-cancellation
    # differences of float-noise (the same two-sided corr-denom class as market_beta). P2 Neumaier cleared the
    # adversarial-seed straddle but the real gappy tape still breached ~0.5% of minutes (autocorr_2 null-flip /
    # value-Δ up to ~4.5e-4): the degenerate cell sat in the [1e-12, ~3e-12] denom/(Σ)² band where the corr is
    # noise/noise and batch-vs-incremental round it differently. CLOSED by the OLS defined-guard floor raise
    # 1e-12→1e-10 (declarative.py _OLS_DENOM_X/Y_REL_EPS): the whole noise/noise band is now NULL on BOTH paths
    # (correct — a flat-return window has no defined autocorrelation), and 1e-10 is 5+ decades below real return
    # variance (CV² min ~4.5e-5) so no well-conditioned window is touched. Value change on the degenerate band
    # only -> version 1.0.0→1.1.0 + re-trust. Now SAFE for the incremental fast path.
    incremental_safe = True

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in AUTOCORR_WINDOWS:
            specs.append(
                FeatureSpec(name=f"autocorr_1_{w}m", description=f"Lag-1 autocorrelation of one-minute returns over {w} minutes (negative = mean-reverting, positive = trending), in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="warmup", layer="A", tolerance=DYN_TOL)
            )
            specs.append(
                FeatureSpec(name=f"autocorr_2_{w}m", description=f"Lag-2 autocorrelation of one-minute returns over {w} minutes (two-step return persistence), in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="warmup", layer="A", tolerance=DYN_TOL)
            )
        for w in ACCEL_WINDOWS:
            specs.append(
                FeatureSpec(name=f"ret_accel_{w}m", description=f"Return acceleration: the trailing {w}-minute return minus the prior {w}-minute return (is the move speeding up or fading).",
                            dtype="Float64", valid_range=(-5.0, 5.0), nan_policy="warmup", layer="A")
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {}  # no mean/std/sum reductions — only OLS (autocorrelation) and point lags

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        close = pl.col("close")
        # ret at t = close[t]/close[t-1]-1; ret_lag1 = ret at t-1 = close[t-1]/close[t-2]-1; etc.
        ret = close / close.shift(1).over("symbol") - 1.0
        ret_lag1 = close.shift(1).over("symbol") / close.shift(2).over("symbol") - 1.0
        ret_lag2 = close.shift(2).over("symbol") / close.shift(3).over("symbol") - 1.0
        return {
            "ac1": (ret_lag1, ret, ("corr",), AUTOCORR_WINDOWS),
            "ac2": (ret_lag2, ret, ("corr",), AUTOCORR_WINDOWS),
        }

    def points(self) -> dict[str, pl.Expr]:
        pts: dict[str, pl.Expr] = {"c": pl.col("close")}
        for lag in ACCEL_LAGS:
            pts[f"l{lag}"] = pl.col("close").shift(lag).over("symbol")
        return pts

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in AUTOCORR_WINDOWS:
            feats[f"autocorr_1_{w}m"] = corr_("ac1", w)
            feats[f"autocorr_2_{w}m"] = corr_("ac2", w)
        for w in ACCEL_WINDOWS:
            recent = pt_("c") / pt_(f"l{w}") - 1.0
            prior = pt_(f"l{w}") / pt_(f"l{2 * w}") - 1.0
            feats[f"ret_accel_{w}m"] = recent - prior
        return feats
