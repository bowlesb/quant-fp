"""Persistent liquidity rank — the name's slow cross-sectional liquidity TIER (family: CROSS_SECTIONAL).

The single most important conditioning variable in this research program's findings: EVERY reversion
edge (price, event, gap-fill) is illiquid-CONCENTRATED and dies in the liquid tier, while the lone
liquid survivor (W11 overnight-beta) is a risk premium. That entire structure is governed by a name's
SLOW, persistent liquidity level — yet the platform only exposes INTRADAY liquidity (amihud/Kyle/Roll
over minute windows) and a NOISY per-minute ``dollar_volume_rank_1m``. Neither captures the persistent
tier a name structurally lives in (a name does not jump liquidity tiers minute to minute). This group
adds the slow, trailing liquidity rank — the variable a model needs to learn "signal X works only in
the top liquidity tertile".

Per (symbol, date), from the daily frame:
  - ``adv_dollar_log_20d`` = log1p of the trailing-20-day mean dollar volume (close * volume) — the raw
    persistent liquidity LEVEL.
  - ``liquidity_rank``     = the symbol's cross-sectional PERCENTILE [0,1] of adv_dollar_20d within the
    day's universe (1 = most liquid). The structural tier, robust to absolute-dollar drift over time.

A DAILY-broadcast group (like ``overnight_intraday_split`` / ``daily_beta``). The cross-sectional rank
is pinned to the day's ``universe`` membership when provided (the SAME pin breadth / return_dispersion
use), so the rank denominator cannot drift between live and backfill. Source-independent (settled daily
bars), so parity-true by construction; ``compute_latest`` reruns the same code on the latest minute.
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

ADV_WINDOW = 20
MIN_DAYS = 10


@register
class LiquidityRankGroup(FeatureGroup):
    name = "liquidity_rank"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close", "volume")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="adv_dollar_log_20d",
                description="log1p of the trailing-20-day mean dollar volume (close*volume) — the persistent liquidity LEVEL.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="liquidity_rank",
                description="The symbol's cross-sectional percentile [0,1] of trailing-20d dollar volume within the day's universe (1 = most liquid) — the structural liquidity tier governing the illiquid-mirage / liquid-survivor split.",
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _daily_features(self, ctx: BatchContext) -> pl.DataFrame:
        """Per (symbol, date) trailing-20d ADV + its cross-sectional liquidity rank. Cached on the
        (daily-snapshot, universe-membership) identity so the (identical-all-day) daily features are
        computed once, not per minute."""
        source = ctx.frame("daily")
        universe = ctx.frames["universe"] if "universe" in ctx.frames else None
        # The rank DENOMINATOR depends on the universe membership, so the cache key pairs the daily-snapshot
        # token with a universe witness — a changed membership re-keys and recomputes (never a stale rank).
        universe_witness: tuple[object, ...] = (
            (id(universe), universe.height) if universe is not None else (None, 0)
        )
        token = (*daily_snapshot_token(source), *universe_witness)
        members = universe.select("symbol").unique() if universe is not None else None
        return self.session_cache.get(token, lambda: self._compute_daily_features(source, members))

    def _compute_daily_features(self, source: pl.DataFrame, members: pl.DataFrame | None) -> pl.DataFrame:
        """The actual per-(symbol, date) daily feature computation (the cached body)."""
        daily = source.select(["symbol", "date", "close", "volume"]).sort(["symbol", "date"])
        daily = daily.with_columns((pl.col("close") * pl.col("volume")).alias("_dvol"))
        daily = daily.with_columns(
            pl.col("_dvol")
            .rolling_mean(window_size=ADV_WINDOW, min_samples=MIN_DAYS)
            .over("symbol")
            .alias("_adv")
        )
        if members is not None:
            daily = daily.join(members, on="symbol", how="inner")
        rank = (pl.col("_adv").rank(method="average") / pl.col("_adv").count()).over(
            "date"
        )
        return daily.with_columns(
            pl.col("_adv").log1p().alias("adv_dollar_log_20d"),
            pl.when(pl.col("_adv").is_not_null())
            .then(rank)
            .otherwise(None)
            .alias("liquidity_rank"),
        ).select(["symbol", "date", "adv_dollar_log_20d", "liquidity_rank"])

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
