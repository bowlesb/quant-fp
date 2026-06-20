"""Overnight vs intraday market beta — the W11 premium's exact asymmetry, as a parity-true feature
(family: CROSS_SECTIONAL).

The CERTIFIED W11 overnight-beta premium is specifically that high-beta names earn the market risk
premium OVERNIGHT (prev_close -> open) while low-beta names earn it INTRADAY (open -> close) — the
edge is the ASYMMETRY between the overnight and intraday beta, not the total daily beta. ``daily_beta``
exposes the total daily beta; this group decomposes it into the two legs the W11 signal trades on,
which ``daily_beta`` cannot reconstruct. Non-redundant by construction, and the highest-relevance
risk-premium feature given W11 is the program's lone liquid edge.

Per (symbol, date), over the trailing 60 days:
  - ``overnight_beta_60d`` = rolling-60d OLS beta of the name's OVERNIGHT return (open/prev_close - 1)
    on SPY's overnight return.
  - ``intraday_beta_60d``  = rolling-60d OLS beta of the name's INTRADAY return (close/open - 1) on
    SPY's intraday return.
  - ``beta_overnight_minus_intraday`` = overnight_beta - intraday_beta — the W11 asymmetry itself
    (positive = the name carries MORE market risk overnight than intraday, the high-overnight-beta leg).

A DAILY-broadcast group (like ``overnight_intraday_split`` / ``daily_beta``). SPY is in the same daily
frame (ordinary symbol, no new data path); the daily frame carries 200d so the 60d window is warm.
Source-independent (settled daily bars), so parity-true by construction; ``compute_latest`` reruns the
same code on the latest minute. NaN until the window has >=20 finite pairs or if the relevant SPY-leg
variance is 0 (beta undefined).
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
class OvernightBetaGroup(FeatureGroup):
    name = "overnight_beta"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "open", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )
    # Per-session cache of the daily features keyed by the daily-snapshot content token. The snapshot is
    # fixed for the whole trading day, so its derived per-(symbol, date) overnight/intraday betas are
    # identical every minute — compute once, broadcast each minute (the recompute-every-minute over the
    # full 200-day daily history was the group's cost). Mirrors daily_beta / multi_day / prior_day.
    _daily_cache: tuple[tuple[int, int, object, float], pl.DataFrame] | None = None

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="overnight_beta_60d",
                description="Rolling 60-day OLS beta of the name's OVERNIGHT return (open/prev_close-1) on SPY's overnight return — the leg high-beta names earn the W11 premium on. NaN if <20 pairs or SPY overnight variance is 0.",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="intraday_beta_60d",
                description="Rolling 60-day OLS beta of the name's INTRADAY return (close/open-1) on SPY's intraday return.",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="beta_overnight_minus_intraday",
                description="overnight_beta_60d - intraday_beta_60d: the W11 asymmetry (positive = the name carries more market risk overnight than intraday).",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _leg_beta(
        self, frame: pl.DataFrame, name_col: str, mkt_col: str, out: str
    ) -> pl.Expr:
        mkt_var = (
            pl.col(mkt_col)
            .rolling_var(window_size=BETA_WINDOW, min_samples=MIN_PAIRS)
            .over("symbol")
        )
        cov_roll = pl.rolling_cov(
            pl.col(name_col),
            pl.col(mkt_col),
            window_size=BETA_WINDOW,
            min_samples=MIN_PAIRS,
        ).over("symbol")
        return pl.when(mkt_var > 0).then(cov_roll / mkt_var).otherwise(None).alias(out)

    def _daily_features(self, ctx: BatchContext) -> pl.DataFrame:
        """Per (symbol, date) rolling-60d overnight/intraday betas to SPY. Cached on the daily-snapshot
        identity so the (identical-all-day) daily features are computed once, not per minute."""
        source = ctx.frame("daily")
        token = daily_snapshot_token(source)
        cached = self._daily_cache
        if cached is not None and cached[0] == token:
            return cached[1]
        result = self._compute_daily_features(source)
        self._daily_cache = (token, result)
        return result

    def _compute_daily_features(self, source: pl.DataFrame) -> pl.DataFrame:
        """The actual per-(symbol, date) daily feature computation (the cached body)."""
        daily = source.select(["symbol", "date", "open", "close"]).sort(["symbol", "date"])
        prev_close = pl.col("close").shift(1).over("symbol")
        daily = daily.with_columns(
            (pl.col("open") / prev_close - 1.0).alias("on_ret"),
            (pl.col("close") / pl.col("open") - 1.0).alias("id_ret"),
        )
        market = (
            daily.filter(pl.col("symbol") == MARKET_TICKER)
            .select(
                [
                    "date",
                    pl.col("on_ret").alias("mkt_on"),
                    pl.col("id_ret").alias("mkt_id"),
                ]
            )
            .sort("date")
        )
        joined = daily.join(market, on="date", how="left").sort(["symbol", "date"])
        return (
            joined.with_columns(
                self._leg_beta(joined, "on_ret", "mkt_on", "overnight_beta_60d"),
                self._leg_beta(joined, "id_ret", "mkt_id", "intraday_beta_60d"),
            )
            .with_columns(
                (pl.col("overnight_beta_60d") - pl.col("intraday_beta_60d")).alias(
                    "beta_overnight_minus_intraday"
                )
            )
            .select(
                [
                    "symbol",
                    "date",
                    "overnight_beta_60d",
                    "intraday_beta_60d",
                    "beta_overnight_minus_intraday",
                ]
            )
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
