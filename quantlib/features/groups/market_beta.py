"""Market-relationship features: how a ticker co-moves with SPY (family: CROSS_SECTIONAL, Layer A).

Per symbol, regress the one-minute return on SPY's one-minute return over each window (shared
windowed-OLS kernel): the slope is rolling beta, the correlation is how tightly it tracks the market,
and idiosyncratic volatility is the part of its movement the market does not explain. SPY is already
in minute_agg (streamed + backfilled as a market-context symbol), so this is pure-bar and parity-true
by construction — the same minute_agg feeds the same regression live and in backfill.
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

WINDOWS: tuple[int, ...] = (10, 15, 30, 45, 60, 90, 120)
MARKET_TICKER = "SPY"


@register
class MarketBetaGroup(FeatureGroup):
    name = "market_beta"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"market_beta_{w}m", description=f"Rolling beta to SPY over {w} minutes: slope of this ticker's one-minute return regressed on SPY's.",
                            dtype="Float64", valid_range=(-15.0, 15.0), nan_policy="sparse", layer="A", tolerance=1e-4)
            )
            specs.append(
                FeatureSpec(name=f"market_corr_{w}m", description=f"Rolling correlation of this ticker's one-minute return with SPY's over {w} minutes, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A", tolerance=1e-4)
            )
            specs.append(
                FeatureSpec(name=f"idio_vol_{w}m", description=f"Idiosyncratic volatility over {w} minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain).",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="sparse", layer="A", tolerance=1e-4)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        frame = frame.with_columns((pl.col("close") / pl.col("_prev") - 1.0).alias("_ret"))
        market = (
            frame.filter(pl.col("symbol") == MARKET_TICKER)
            .select(["minute", pl.col("_ret").alias("_mret")])
        )
        frame = frame.join(market, on="minute", how="left").sort(["symbol", "minute"])
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            fit = ols_window_exprs("_mret", "_ret", size)
            ret_std = pl.col("_ret").rolling_std_by("minute", window_size=size).over("symbol")
            idio = ret_std * (1.0 - fit["r2"]).clip(0.0, 1.0).sqrt()
            exprs.append(fit["slope"].cast(pl.Float64).alias(f"market_beta_{w}m"))
            exprs.append(fit["corr"].cast(pl.Float64).alias(f"market_corr_{w}m"))
            exprs.append(idio.cast(pl.Float64).alias(f"idio_vol_{w}m"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("market_beta", "market_corr", "idio_vol")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
