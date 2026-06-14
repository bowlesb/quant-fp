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
from quantlib.features.latest import pivot_stat, rust_reductions, windowed_ols_latest
from quantlib.features.ols import with_ols_columns
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
        r2_cols = []
        for w in WINDOWS:
            size = f"{w}m"
            r2_col = f"_r2_{w}"
            frame = with_ols_columns(
                frame, "_mret", "_ret", size,
                {"slope": f"market_beta_{w}m", "corr": f"market_corr_{w}m", "r2": r2_col},
            )
            r2_cols.append(r2_col)
        idio = []
        for w in WINDOWS:
            ret_std = pl.col("_ret").rolling_std_by("minute", window_size=f"{w}m").over("symbol")
            idio.append((ret_std * (1.0 - pl.col(f"_r2_{w}")).clip(0.0, 1.0).sqrt()).cast(pl.Float64).alias(f"idio_vol_{w}m"))
        frame = frame.with_columns(idio).drop(r2_cols)
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("market_beta", "market_corr", "idio_vol")]
        return frame.select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: rolling beta/corr vs SPY via aggregate-at-T OLS; idio = ret std * sqrt(1-r2)."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        frame = frame.with_columns((pl.col("close") / pl.col("_prev") - 1.0).alias("_ret"))
        market = frame.filter(pl.col("symbol") == MARKET_TICKER).select(["minute", pl.col("_ret").alias("_mret")])
        frame = frame.join(market, on="minute", how="left").sort(["symbol", "minute"])
        latest = frame["minute"].max()
        long = windowed_ols_latest(frame, "_mret", "_ret", WINDOWS)
        beta = pivot_stat(long, "slope", "market_beta_{w}m", WINDOWS)
        corr = pivot_stat(long, "corr", "market_corr_{w}m", WINDOWS)
        r2 = pivot_stat(long, "r2", "_r2_{w}", WINDOWS)
        ret_std = pivot_stat(rust_reductions(frame, "_ret", WINDOWS), "std", "_rstd_{w}", WINDOWS)
        out = (
            frame.filter(pl.col("minute") == latest).select("symbol")
            .join(beta, on="symbol", how="left").join(corr, on="symbol", how="left")
            .join(r2, on="symbol", how="left").join(ret_std, on="symbol", how="left")
        )
        out = out.with_columns(
            [(pl.col(f"_rstd_{w}") * (1.0 - pl.col(f"_r2_{w}")).clip(0.0, 1.0).sqrt()).cast(pl.Float64).alias(f"idio_vol_{w}m") for w in WINDOWS]
        ).with_columns(pl.lit(latest).alias("minute"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("market_beta", "market_corr", "idio_vol")]
        return out.select(["symbol", "minute", *names])
