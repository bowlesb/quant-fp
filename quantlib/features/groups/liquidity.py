"""Liquidity / trading-cost features from per-minute bars + signed flow (family: TRADE_FLOW, Layer B).

Three classic illiquidity estimators over each window:
- **Amihud illiquidity** — mean of |return| / dollar-volume: how much price moves per dollar traded.
- **Roll implied spread** — 2*sqrt(-cov(dp, dp_-1)) / price: the effective spread implied by negative
  autocovariance of consecutive price changes (0 when the autocovariance is non-negative).
- **Kyle's lambda** — slope of price change on signed order flow (via the OLS kernel): price impact
  per share of net buying/selling.

Amihud/Roll are bar-only; Kyle uses tick-rule ``signed_volume`` (so the group is Layer B, same parity
profile as trade_flow). All from time-anchored rolling sums -> identical live and backfill.
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

WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)


@register
class LiquidityGroup(FeatureGroup):
    name = "liquidity"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume", "signed_volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"amihud_illiq_{w}m", description=f"Amihud illiquidity over {w} minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded).",
                            dtype="Float64", valid_range=(0.0, None), nan_policy="warmup", layer="B")
            )
            specs.append(
                FeatureSpec(name=f"roll_spread_{w}m", description=f"Roll implied effective spread over {w} minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative.",
                            dtype="Float64", valid_range=(0.0, 1.0), nan_policy="warmup", layer="B")
            )
            specs.append(
                FeatureSpec(name=f"kyle_lambda_{w}m", description=f"Kyle's lambda over {w} minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid.",
                            dtype="Float64", nan_policy="warmup", layer="B", tolerance=1e-4)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "volume", "signed_volume"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        dp = pl.col("close") - pl.col("_prev")
        dollar = pl.col("close") * pl.col("volume")
        abs_ret = (pl.col("close") / pl.col("_prev") - 1.0).abs()
        frame = frame.with_columns([dp.alias("_dp"), (abs_ret / dollar).alias("_amihud_term")])
        frame = lagged(frame, "_dp", 1, "_dp_lag").sort(["symbol", "minute"])
        both = pl.col("_dp").is_not_null() & pl.col("_dp_lag").is_not_null()
        dp_z = pl.when(both).then(pl.col("_dp")).otherwise(0.0)
        dpl_z = pl.when(both).then(pl.col("_dp_lag")).otherwise(0.0)
        frame = frame.with_columns(
            [both.cast(pl.Float64).alias("_pair"), dp_z.alias("_dpz"), dpl_z.alias("_dplz"),
             (dp_z * dpl_z).alias("_dpprod")]
        )
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            amihud = pl.col("_amihud_term").rolling_mean_by("minute", window_size=size).over("symbol")
            n = pl.col("_pair").rolling_sum_by("minute", window_size=size).over("symbol")
            s_dp = pl.col("_dpz").rolling_sum_by("minute", window_size=size).over("symbol")
            s_dpl = pl.col("_dplz").rolling_sum_by("minute", window_size=size).over("symbol")
            s_prod = pl.col("_dpprod").rolling_sum_by("minute", window_size=size).over("symbol")
            cov = pl.when(n >= 2.0).then(s_prod / n - (s_dp / n) * (s_dpl / n)).otherwise(None)
            roll = pl.when(cov < 0.0).then(2.0 * (-cov).sqrt() / pl.col("close")).otherwise(0.0)
            kyle = ols_window_exprs("signed_volume", "_dp", size)["slope"]
            exprs.append(amihud.cast(pl.Float64).alias(f"amihud_illiq_{w}m"))
            exprs.append(roll.cast(pl.Float64).alias(f"roll_spread_{w}m"))
            exprs.append(kyle.cast(pl.Float64).alias(f"kyle_lambda_{w}m"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("amihud_illiq", "roll_spread", "kyle_lambda")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
