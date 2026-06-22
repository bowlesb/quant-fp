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
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # The autocorrelation OLS regressor x is the LAGGED one-minute return; the corr denom x-side is conditioned
    # by the shared variance-scale guard (_OLS_DENOM_X_CENTERED_REL_EPS, #416). The last residual breach was a
    # SHARED-ENGINE artifact, not this group's math: when price_volume's obv time regression co-resided in the
    # IncrementalEngine, the per-minute rebase_time_axis realized the Neumaier compensation across the WHOLE
    # value array, collapsing a flat-name Σxx-exactly-zero cell into a ~1e-22 residue → a spurious autocorr_2_10m
    # where the batch NULLs (real-soak 2026-06-17: 4/779 min, ONLY under FP_RUST_REDUCE with price_volume armed).
    # FIXED in WindowedSumState.rebase_time_axis (realize only the time-OLS columns it shifts), so a co-resident
    # time group no longer perturbs this group — engine==batch is CLEAN on the real-tape soak at BOTH FR=0 and
    # FR=1 (17/17 and 18/18 ALL-GO, worst tol-ratio 0.00; guarded by
    # test_co_resident_time_ols_group_does_not_perturb_unanchored_group). Now SAFE for the incremental fast path.
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
