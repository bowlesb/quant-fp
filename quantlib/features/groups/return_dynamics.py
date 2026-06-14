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
from quantlib.features.latest import pivot_stat, windowed_ols_latest
from quantlib.features.ols import with_ols_columns
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
        # Each distinct close-lag is a self-join; compute each ONCE (dedup w/2w overlaps) and derive
        # the return-lags from the close-lags instead of two more joins on _ret. _ret_lag1 at t is the
        # return at t-1 = close[t-1]/close[t-2] - 1, etc — byte-identical to lagging _ret directly.
        lags = sorted({1, 2, 3} | {w for w in ACCEL_WINDOWS} | {2 * w for w in ACCEL_WINDOWS})
        for offset in lags:
            frame = lagged(frame, "close", offset, f"_lag{offset}")
        frame = frame.sort(["symbol", "minute"])
        frame = frame.with_columns(
            [
                (pl.col("close") / pl.col("_lag1") - 1.0).alias("_ret"),
                (pl.col("_lag1") / pl.col("_lag2") - 1.0).alias("_ret_lag1"),
                (pl.col("_lag2") / pl.col("_lag3") - 1.0).alias("_ret_lag2"),
            ]
        )
        for w in AUTOCORR_WINDOWS:
            size = f"{w}m"
            frame = with_ols_columns(frame, "_ret_lag1", "_ret", size, {"corr": f"autocorr_1_{w}m"})
            frame = with_ols_columns(frame, "_ret_lag2", "_ret", size, {"corr": f"autocorr_2_{w}m"})
        exprs = []
        for w in ACCEL_WINDOWS:
            recent = pl.col("close") / pl.col(f"_lag{w}") - 1.0
            prior = pl.col(f"_lag{w}") / pl.col(f"_lag{2 * w}") - 1.0
            exprs.append((recent - prior).cast(pl.Float64).alias(f"ret_accel_{w}m"))
        names = (
            [f"autocorr_{lag}_{w}m" for w in AUTOCORR_WINDOWS for lag in (1, 2)]
            + [f"ret_accel_{w}m" for w in ACCEL_WINDOWS]
        )
        return frame.with_columns(exprs).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: lag-1/2 autocorrelation via aggregate-at-T OLS; return acceleration via point
        lookups of the lagged closes on the T row."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        lags = sorted({1, 2, 3} | {w for w in ACCEL_WINDOWS} | {2 * w for w in ACCEL_WINDOWS})
        for offset in lags:
            frame = lagged(frame, "close", offset, f"_lag{offset}")
        frame = frame.sort(["symbol", "minute"]).with_columns(
            [
                (pl.col("close") / pl.col("_lag1") - 1.0).alias("_ret"),
                (pl.col("_lag1") / pl.col("_lag2") - 1.0).alias("_ret_lag1"),
                (pl.col("_lag2") / pl.col("_lag3") - 1.0).alias("_ret_lag2"),
            ]
        )
        latest = frame["minute"].max()
        ac1 = pivot_stat(windowed_ols_latest(frame, "_ret_lag1", "_ret", AUTOCORR_WINDOWS), "corr", "autocorr_1_{w}m", AUTOCORR_WINDOWS)
        ac2 = pivot_stat(windowed_ols_latest(frame, "_ret_lag2", "_ret", AUTOCORR_WINDOWS), "corr", "autocorr_2_{w}m", AUTOCORR_WINDOWS)
        accel = frame.filter(pl.col("minute") == latest).select(
            ["symbol", *[((pl.col("close") / pl.col(f"_lag{w}") - 1.0) - (pl.col(f"_lag{w}") / pl.col(f"_lag{2 * w}") - 1.0)).cast(pl.Float64).alias(f"ret_accel_{w}m") for w in ACCEL_WINDOWS]]
        )
        out = accel.join(ac1, on="symbol", how="left").join(ac2, on="symbol", how="left").with_columns(pl.lit(latest).alias("minute"))
        names = (
            [f"autocorr_{lag}_{w}m" for w in AUTOCORR_WINDOWS for lag in (1, 2)]
            + [f"ret_accel_{w}m" for w in ACCEL_WINDOWS]
        )
        return out.select(["symbol", "minute", *names])
