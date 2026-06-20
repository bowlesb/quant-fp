"""Overnight vs intraday return split — the decomposition the certified W11 overnight-beta lives on.

A daily return decomposes into an OVERNIGHT leg (prev_close → today_open) and an INTRADAY leg
(today_open → today_close). The W11 certification proved this split CARRIES signal (high-beta names earn the
premium OVERNIGHT, not intraday). The platform already exposes ``gap_open`` (= the overnight leg) and
``dist_from_prior_close`` (= prev_close → close), but NOT the INTRADAY leg on its own, nor the overnight/
intraday ASYMMETRY — which is the exact quantity the W11 split exploits and a model would otherwise have to
learn as a nonlinear combination. This group adds those non-redundant pieces (family: PRICE, Layer A):

  - ``intraday_ret``             = today_close / today_open − 1 (the open→close leg).
  - ``overnight_minus_intraday`` = gap_open − intraday_ret (the asymmetry: does the name give back intraday
    what it gapped overnight, or extend it — the overnight/intraday tug-of-war).
  - ``overnight_share``          = |overnight| / (|overnight| + |intraday|) — how much of today's absolute
    move happened overnight (NaN on a zero-move day).

A DAILY-broadcast group (like ``prior_day``): the daily features are computed per (symbol, date) then joined
onto every minute of that day, so the output is keyed (symbol, minute). Point-in-time from the daily bar
(open, close, prev_close), parity-true by construction (``compute_latest`` reruns the same code on the latest
minute — auto-guarded by tests/test_fp_latest.py).
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


@register
class OvernightIntradaySplitGroup(FeatureGroup):
    name = "overnight_intraday_split"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "open", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="intraday_ret",
                description="Intraday return: today's close relative to today's open (close/open - 1) — the open-to-close leg.",
                dtype="Float64",
                valid_range=(-1.0, 5.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="overnight_minus_intraday",
                description="Overnight/intraday asymmetry: the overnight gap (open/prev_close-1) minus the intraday return (close/open-1).",
                dtype="Float64",
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="overnight_share",
                description="Overnight share of the day's absolute move: |overnight| / (|overnight| + |intraday|); NaN on a zero-move day.",
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _daily_features(self, ctx: BatchContext) -> pl.DataFrame:
        """Per (symbol, date) overnight/intraday split features from the daily bar. Cached on the daily-
        snapshot identity so the (identical-all-day) daily features are computed once, not per minute."""
        source = ctx.frame("daily")
        return self.session_cache.get(
            daily_snapshot_token(source), lambda: self._compute_daily_features(source)
        )

    def _compute_daily_features(self, source: pl.DataFrame) -> pl.DataFrame:
        daily = source.select(self.inputs[0].columns).sort(["symbol", "date"])
        daily = daily.with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        overnight = pl.col("open") / pl.col("prev_close") - 1.0
        intraday = pl.col("close") / pl.col("open") - 1.0
        abs_total = overnight.abs() + intraday.abs()
        return daily.with_columns(
            intraday.alias("intraday_ret"),
            (overnight - intraday).alias("overnight_minus_intraday"),
            pl.when(abs_total > 0).then(overnight.abs() / abs_total).otherwise(None).alias("overnight_share"),
        ).select(["symbol", "date", "intraday_ret", "overnight_minus_intraday", "overnight_share"])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        minutes = ctx.frame("minute_agg").select(["symbol", "minute"]).with_columns(
            pl.col("minute").dt.date().alias("date")
        )
        joined = minutes.join(self._daily_features(ctx), on=["symbol", "date"], how="left")
        return joined.select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Same code on a minute_agg restricted to the latest minute — the daily computation is identical;
        only the broadcast shrinks from all minutes to one (parity-true by construction)."""
        minute_agg = ctx.frame("minute_agg")
        latest = minute_agg["minute"].max()
        sub = BatchContext(frames={**ctx.frames, "minute_agg": minute_agg.filter(pl.col("minute") == latest)})
        return self.compute(sub)
