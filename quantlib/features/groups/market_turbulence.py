"""Market turbulence — universe-wide realized-move-magnitude scalars (family: CROSS_SECTIONAL).

A GATHER group, computed ONCE per minute over the WHOLE universe and broadcast/joined to every ticker —
the same per-minute universe reduce as ``breadth``/``market_context``, not a per-symbol fold. For each
minute ``T`` it collapses the cross-section to a handful of market-state SCALARS:

  * ``mkt_absret_{W}m`` = equal-weight mean over the universe of ``|close[T]/close[T-W] - 1|`` — the
    realized whole-market MOVE MAGNITUDE over the trailing ``W`` minutes (turbulence, NOT direction);
  * ``mkt_rv_30m``      = equal-weight mean of each symbol's trailing-30m realized vol (std of 1m log
    returns over (T-30, T]) — the universe-mean realized volatility level as of ``T``.

WHY THESE, AND WHY NOT DIRECTION. The Lane-D regime screen
(``experiments/2026-06-20-signal-source-expansion/regime_screen.py``) conditioned the market state at T
on the forward CROSS-SECTIONAL AGGREGATE at T+h (the correct test for a broadcast scalar — a within-
minute rank-IC is structurally null for a value that is identical across names). After partialling out
universe own-vol (``mkt_rv_30m``) from both sides, the eye-popping raw correlations collapsed to vol
PERSISTENCE already captured by per-symbol vol features — EXCEPT the universe mean-|return| turbulence
scalar, which survives the own-vol control: it predicts forward VOLUME (partial r~0.20, collapse-ratio
>1.0, i.e. NOT a vol-persistence artifact) and a residual forward move-magnitude (partial r~0.30),
OOS-sign-consistent across an early/late day split. Direction is null here (``mkt_ret``/``spy_ret``
partial |r|~0.10, the floor), consistent with the three settled cross-sectional direction-nulls. So this
group emits the vol/intensity scalars the signal actually lives in, not signed market direction.

PARITY BY CONSTRUCTION. The value at minute ``T`` depends only on that minute's per-symbol trailing
returns / realized vol, reduced over the universe with order-independent aggregates (``mean``), so the
live aggregate-at-T form equals the backfill rolling form cell-for-cell — exactly like ``breadth``'s
gather (proven by ``tests/test_fp_market_turbulence.py`` + the generic latest-parity test). Unlike
breadth there is NO discontinuous sign count, so no dead-band is needed; ``|return|`` and a realized-vol
std are continuous in the inputs, so cell tolerance composes into the mean directly.

DENOMINATOR = symbols with a VALID measurement that minute (for ``mkt_absret_W``: a close at BOTH ``T``
and ``T-W``; for ``mkt_rv_30m``: at least 10 valid 1m log returns in the trailing 30m), computed
identically both sides — nulls are auto-excluded by polars ``mean``. Pinned to the day's ``universe``
membership when provided so the denominator cannot drift (the same pin breadth/market_context use).
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
from quantlib.features.registry import register

ABSRET_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)  # trailing |return| horizons, in minutes
RV_WINDOW: int = 30  # realized-vol window (minutes) — std of 1m log returns over (T-RV_WINDOW, T]
RV_MIN_OBS: int = 10  # minimum valid 1m log returns in the RV window for a defined per-symbol RV


@register
class MarketTurbulenceGroup(FeatureGroup):
    name = "market_turbulence"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for window in ABSRET_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"mkt_absret_{window}m",
                    description=f"Universe equal-weight mean of |trailing-{window}m return| — the realized whole-market move magnitude (turbulence, not direction); a market-wide scalar broadcast to every ticker.",
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="sparse",
                    layer="A",
                )
            )
        specs.append(
            FeatureSpec(
                name=f"mkt_rv_{RV_WINDOW}m",
                description=f"Universe equal-weight mean of each symbol's trailing-{RV_WINDOW}m realized volatility (std of 1m log returns); the market-wide realized-vol level broadcast to every ticker.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            )
        )
        return specs

    def reduce_buffer_minutes(self) -> int | None:
        """A universe-wide GATHER (runs in the reader's reduce phase, not per shard), so the reader's
        minimal reduce ring must be deep enough for it. The deepest lookback is the longest trailing
        |return| horizon (``max(ABSRET_WINDOWS)``), which also covers the RV window (``RV_WINDOW`` <=
        that max)."""
        return max(ABSRET_WINDOWS)

    def _pin_universe(self, ctx: BatchContext, measures: pl.DataFrame) -> pl.DataFrame:
        """Pin the per-(symbol, minute) measures to the day's universe membership when provided, so the
        turbulence denominator cannot drift between live and backfill (the same pin breadth uses). Without
        a universe frame the reduce runs over whatever printed (coverage-gated, like cross_sectional_rank).
        """
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            return measures.join(members, on="symbol", how="inner")
        return measures

    def _abs_returns(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) ``|trailing-W return|`` over each ABSRET_WINDOWS horizon, as
        ``_absret_{W}m`` columns. A cell is null where the bar exactly ``W`` minutes ago is absent
        (``lagged`` is time-based) — null = not a valid return, excluded from the mean both sides."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for window in ABSRET_WINDOWS:
            frame = lagged(frame, "close", window, f"_lag{window}")
        frame = frame.sort(["symbol", "minute"])
        return frame.with_columns(
            [
                (pl.col("close") / pl.col(f"_lag{window}") - 1.0).abs().alias(f"_absret_{window}m")
                for window in ABSRET_WINDOWS
            ]
        ).select(["symbol", "minute", *[f"_absret_{window}m" for window in ABSRET_WINDOWS]])

    def _realized_vol(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) trailing-``RV_WINDOW`` realized vol = std of the 1m log returns over
        ``(T-RV_WINDOW, T]``, as ``_rv{RV_WINDOW}`` — a time-based rolling std over each symbol's logret
        series, defined only with at least ``RV_MIN_OBS`` valid returns (else null, excluded from the
        universe mean both sides). 1m log returns are point-in-time: a return is valid only across an
        EXACT one-minute step with both closes positive, so a gap does not splice a multi-minute jump into
        the vol."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        logret = (
            frame.with_columns(
                pl.col("close").shift(1).over("symbol").alias("_prev_close"),
                pl.col("minute").shift(1).over("symbol").alias("_prev_minute"),
            )
            .with_columns(
                pl.when(
                    (pl.col("_prev_close") > 0)
                    & (pl.col("close") > 0)
                    & ((pl.col("minute") - pl.col("_prev_minute")) == pl.duration(minutes=1))
                )
                .then((pl.col("close") / pl.col("_prev_close")).log())
                .otherwise(None)
                .alias("_logret")
            )
            .select(["symbol", "minute", "_logret"])
        )
        # Trailing realized vol = a time-based rolling std of the 1m log returns over the window ending at
        # each minute: ``(T-RV_WINDOW, T]`` (closed-right), which on the 1m end-minute grid captures the
        # logrets at T, T-1, ..., T-(RV_WINDOW-1) — the same RV_WINDOW observations the per-minute std
        # reduces. A null logret (gap / non-positive close) is dropped from the window by polars, so the
        # vol is over VALID returns only; defined when at least ``RV_MIN_OBS`` of them are present.
        logret = logret.sort(["symbol", "minute"])
        return logret.with_columns(
            pl.col("_logret")
            .rolling_std_by("minute", window_size=f"{RV_WINDOW}m", min_samples=RV_MIN_OBS, closed="right")
            .over("symbol")
            .alias(f"_rv{RV_WINDOW}")
        ).select(["symbol", "minute", f"_rv{RV_WINDOW}"])

    def _reduce(self, measures: pl.DataFrame) -> pl.DataFrame:
        """The GATHER: from per-(symbol, minute) measures, the universe equal-weight mean per minute. Each
        ``mkt_absret_W``/``mkt_rv`` is a mean over names with a VALID measurement that minute (nulls
        auto-excluded by polars ``mean``). UNIQUE reduce aliases (``mktabs*``/``mktrv*`` namespace) so the
        unified-emit gather cannot collide with another group's reduce keys."""
        aggs: list[pl.Expr] = [
            pl.col(f"_absret_{window}m").mean().alias(f"mkt_absret_{window}m") for window in ABSRET_WINDOWS
        ]
        aggs.append(pl.col(f"_rv{RV_WINDOW}").mean().alias(f"mkt_rv_{RV_WINDOW}m"))
        return measures.group_by("minute").agg(aggs)

    def _assemble(self, ctx: BatchContext, minute_keys: pl.DataFrame) -> pl.DataFrame:
        """Compute the per-minute universe turbulence reduce and broadcast/join it onto ``minute_keys`` (the
        (symbol, minute) cells to emit). Shared by compute() and compute_latest() — only the set of minutes
        differs, which is what makes the live form parity-true with the backfill form."""
        abs_returns = self._abs_returns(ctx)
        realized_vol = self._realized_vol(ctx)
        measures = abs_returns.join(realized_vol, on=["symbol", "minute"], how="full", coalesce=True)
        measures = self._pin_universe(ctx, measures)
        market = self._reduce(measures)

        names = [spec.name for spec in self.declare()]
        out = minute_keys.join(market, on="minute", how="left")
        exprs = [pl.col(name).cast(pl.Float64).alias(name) for name in names]
        return out.with_columns(exprs).select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        return self._assemble(ctx, minute_keys)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE gather: the turbulence at minute T depends only on THAT minute's per-symbol
        measures, so the reduce is identical to compute() — we just emit only the latest minute's rows.
        The measures are still built over the buffer (lagged join / RV window), then the reduce + broadcast
        run unchanged, so compute_latest == compute().last by construction (guarded by tests/test_fp_latest).
        """
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = minute_keys["minute"].max()
        return self._assemble(ctx, minute_keys.filter(pl.col("minute") == latest))
