"""Daily market beta — the rolling multi-day beta of DAILY returns to SPY (family: CROSS_SECTIONAL).

This is the core quantity of the CERTIFIED W11 overnight-beta premium (the program's lone liquid-
surviving edge: high-DAILY-beta names earn the market risk premium overnight, low-beta intraday; see
strategies/lib/overnight_beta_model.py + experiments/2026-06-16-w11-overnight-beta/). The platform
exposes INTRADAY minute-beta (``market_beta_*m`` — slope of one-MINUTE returns on SPY) but NOT the
DAILY multi-day beta, which is a DIFFERENT quantity: the W11 premium lives specifically in the daily
risk loading, not the intraday co-movement. A model that wants to learn the W11 regime (or any
beta-conditional structure) needs the daily beta as a feature, and cannot reconstruct it from the
minute-beta. Non-redundant by construction.

Per (symbol, date), over the trailing ``BETA_WINDOW`` daily returns (60 = the W11-certified value):
  - ``daily_beta_60d``     = OLS slope of the name's daily returns on SPY's daily returns
    (= cov(name, mkt)/var(mkt), exactly the W11 ``compute_beta``). NaN if <20 finite pairs or SPY var=0.
  - ``daily_corr_60d``     = correlation of the name's daily returns with SPY's (how much of the move
    is market, in [-1, 1]).
  - ``daily_idio_vol_60d`` = the name's daily-return std × sqrt(1 - corr^2) — the idiosyncratic daily
    risk SPY does not explain.

A DAILY-broadcast group (like ``overnight_intraday_split``): the daily features are computed per
(symbol, date) from the 200-day ``daily`` frame, then joined onto every minute of that day, so the
output is keyed (symbol, minute). SPY sits in the same ``daily`` frame (ingested as an ordinary
symbol), so no new data path. Source-independent (settled daily bars), so parity-true by construction;
``compute_latest`` reruns the same code on the latest minute.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    daily_snapshot_token,
)
from quantlib.features.registry import register

MARKET_TICKER = "SPY"
BETA_WINDOW = 60
MIN_PAIRS = 20


@register
class DailyBetaGroup(FeatureGroup):
    name = "daily_beta"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="daily_beta_60d",
                description="Rolling 60-day OLS beta of the name's DAILY returns on SPY's daily returns (the certified W11 overnight-beta quantity; cov(name,mkt)/var(mkt)). NaN if <20 finite pairs or SPY variance is 0.",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="daily_corr_60d",
                description="Rolling 60-day correlation of the name's daily returns with SPY's, in [-1, 1] — how much of the daily move is market-driven.",
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="daily_idio_vol_60d",
                description="Idiosyncratic daily volatility over 60 days: the name's daily-return std times sqrt(1 - corr^2) — the daily risk SPY does not explain.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _daily_features(self, ctx: BatchContext) -> pl.DataFrame:
        """Per (symbol, date) rolling-60d beta/corr/idio-vol to SPY from the daily frame. Cached on the
        daily-snapshot identity so the (identical-all-day) daily features are computed once, not per minute."""
        source = ctx.frame("daily")
        return self.session_cache.get(
            daily_snapshot_token(source), lambda: self._compute_daily_features(source)
        )

    def _compute_daily_features(self, source: pl.DataFrame) -> pl.DataFrame:
        """The actual per-(symbol, date) daily feature computation (the cached body)."""
        daily = source.select(["symbol", "date", "close"]).sort(["symbol", "date"])
        daily = daily.with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias(
                "ret"
            )
        )

        market = (
            daily.filter(pl.col("symbol") == MARKET_TICKER)
            .select(["date", pl.col("ret").alias("mkt_ret")])
            .sort("date")
        )
        joined = daily.join(market, on="date", how="left").sort(["symbol", "date"])

        mkt_var = (
            pl.col("mkt_ret")
            .rolling_var(window_size=BETA_WINDOW, min_samples=MIN_PAIRS)
            .over("symbol")
        )
        cov_roll = pl.rolling_cov(
            pl.col("ret"),
            pl.col("mkt_ret"),
            window_size=BETA_WINDOW,
            min_samples=MIN_PAIRS,
        ).over("symbol")
        # clip to [-1, 1]: rolling_corr can return 1+eps on a perfectly linear pair (float rounding).
        corr = (
            pl.rolling_corr(
                pl.col("ret"),
                pl.col("mkt_ret"),
                window_size=BETA_WINDOW,
                min_samples=MIN_PAIRS,
            )
            .over("symbol")
            .clip(lower_bound=-1.0, upper_bound=1.0)
        )
        ret_std = (
            pl.col("ret")
            .rolling_std(window_size=BETA_WINDOW, min_samples=MIN_PAIRS)
            .over("symbol")
        )

        beta = pl.when(mkt_var > 0).then(cov_roll / mkt_var).otherwise(None)
        idio = (
            pl.when(corr.is_not_null())
            .then(ret_std * (1.0 - corr * corr).clip(lower_bound=0.0).sqrt())
            .otherwise(None)
        )

        return joined.with_columns(
            beta.alias("daily_beta_60d"),
            corr.alias("daily_corr_60d"),
            idio.alias("daily_idio_vol_60d"),
        ).select(
            ["symbol", "date", "daily_beta_60d", "daily_corr_60d", "daily_idio_vol_60d"]
        )

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        minutes = (
            ctx.frame("minute_agg")
            .select(["symbol", "minute"])
            .with_columns(pl.col("minute").dt.date().alias("date"))
        )
        joined = minutes.join(
            self._daily_features(ctx), on=["symbol", "date"], how="left"
        )
        return joined.select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        minute_agg = ctx.frame("minute_agg")
        latest = minute_agg["minute"].max()
        sub = BatchContext(
            frames={
                **ctx.frames,
                "minute_agg": minute_agg.filter(pl.col("minute") == latest),
            }
        )
        return self.compute(sub)
