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
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    lagged,
)
from quantlib.features.ols import ols_window_exprs
from quantlib.features.registry import register

AUTOCORR_WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
ACCEL_WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60)
DYN_TOL = 1e-4


@register
class ReturnDynamicsGroup(FeatureGroup):
    name = "return_dynamics"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

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

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev1")
        for w in ACCEL_WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
            frame = lagged(frame, "close", 2 * w, f"_lag{2 * w}")
        frame = frame.sort(["symbol", "minute"])
        frame = frame.with_columns((pl.col("close") / pl.col("_prev1") - 1.0).alias("_ret"))
        frame = lagged(frame, "_ret", 1, "_ret_lag1")
        frame = lagged(frame, "_ret", 2, "_ret_lag2").sort(["symbol", "minute"])
        exprs = []
        for w in AUTOCORR_WINDOWS:
            size = f"{w}m"
            exprs.append(ols_window_exprs("_ret_lag1", "_ret", size)["corr"].cast(pl.Float64).alias(f"autocorr_1_{w}m"))
            exprs.append(ols_window_exprs("_ret_lag2", "_ret", size)["corr"].cast(pl.Float64).alias(f"autocorr_2_{w}m"))
        for w in ACCEL_WINDOWS:
            recent = pl.col("close") / pl.col(f"_lag{w}") - 1.0
            prior = pl.col(f"_lag{w}") / pl.col(f"_lag{2 * w}") - 1.0
            exprs.append((recent - prior).cast(pl.Float64).alias(f"ret_accel_{w}m"))
        names = (
            [f"autocorr_{lag}_{w}m" for w in AUTOCORR_WINDOWS for lag in (1, 2)]
            + [f"ret_accel_{w}m" for w in ACCEL_WINDOWS]
        )
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
