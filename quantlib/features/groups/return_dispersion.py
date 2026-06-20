"""Cross-sectional return DISPERSION — how spread-out are the universe's returns right now (family:
CROSS_SECTIONAL).

A GATHER group (the same per-minute universe reduce + broadcast as ``breadth`` / ``market_context``): for each
horizon it computes the **standard deviation** and the **inter-quartile range** of the universe's returns over
that window, as a market-wide scalar broadcast to every ticker. Distinct from ``breadth`` (a sign-COUNT of
up/down names) and ``market_context`` (an index LEVEL): dispersion measures the WIDTH of the cross-section —
high dispersion = a stock-picking regime (names moving independently); low dispersion = a macro / beta regime
(names moving together).

Why it matters: the certified W11 overnight-beta premium is REGIME-CONDITIONAL (it paid in 2025-H2 / 2026-H1
but NOT the 2025-H1 bull half). Cross-sectional dispersion is a natural regime variable the all-features model
can learn that conditionality from (e.g. "the beta premium pays when dispersion is high"). A real,
non-redundant cross-sectional state — no dispersion measure exists in the 610-feature set.

Parity: unlike breadth (which counts ``sign(return)``, discontinuous → needs a dead-band), dispersion is a
std / IQR of the returns themselves — CONTINUOUS in the inputs, so a cell-tolerance difference composes into a
cell-tolerance difference in the aggregate. No dead-band needed; parity-true by construction (the universe is
pinned to the day's membership when provided, the same pin breadth/market_context use, so the denominator
cannot drift between live and backfill). ``compute_latest`` reruns the same reduce on the latest minute.
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
    lagged,
)
from quantlib.features.registry import register

MINUTE_WINDOWS: tuple[int, ...] = (5, 30, 60)
DAY_WINDOWS: tuple[int, ...] = (1, 5)


def _tag(window: int, is_daily: bool) -> str:
    return f"{window}d" if is_daily else f"{window}m"


@register
class ReturnDispersionGroup(FeatureGroup):
    name = "return_dispersion"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
        InputSpec(name="daily", columns=("symbol", "date", "close")),
    )
    # Per-session cache of the DAILY-horizon returns keyed by the daily-snapshot content token. The daily
    # snapshot is fixed all day, so its per-(symbol, date) daily returns are identical every minute — the
    # intraday half (_minute_returns) still recomputes per minute. Mirrors multi_day / daily_beta.
    _daily_cache: tuple[tuple[int, int, object, float], pl.DataFrame] | None = None

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for tag in self._tags():
            specs.append(
                FeatureSpec(
                    name=f"return_dispersion_std_{tag}",
                    description=(
                        f"Cross-sectional standard deviation of the universe's returns over {tag} — a "
                        f"market-wide scalar broadcast to every ticker (high = stock-picking regime, low = "
                        f"macro/beta regime)."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="sparse",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"return_dispersion_iqr_{tag}",
                    description=(
                        f"Cross-sectional inter-quartile range (p75-p25) of the universe's returns over "
                        f"{tag} — a robust dispersion scalar broadcast to every ticker."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="sparse",
                    layer="A",
                )
            )
        return specs

    def _tags(self) -> list[str]:
        return [_tag(w, False) for w in MINUTE_WINDOWS] + [_tag(w, True) for w in DAY_WINDOWS]

    def reduce_buffer_minutes(self) -> int | None:
        """Universe-wide GATHER — runs in the reader's reduce phase; the deepest minute lookback is the
        longest intraday horizon (the daily horizons read the settled daily snapshot, no minute depth)."""
        return max(MINUTE_WINDOWS)

    def _pin_universe(self, ctx: BatchContext, returns: pl.DataFrame) -> pl.DataFrame:
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            return returns.join(members, on="symbol", how="inner")
        return returns

    def _minute_returns(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for window in MINUTE_WINDOWS:
            frame = lagged(frame, "close", window, f"_lag{window}")
        frame = frame.sort(["symbol", "minute"])
        return frame.with_columns(
            [(pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_ret_{_tag(w, False)}") for w in MINUTE_WINDOWS]
        ).select(["symbol", "minute", *[f"_ret_{_tag(w, False)}" for w in MINUTE_WINDOWS]])

    def _daily_returns(self, ctx: BatchContext) -> pl.DataFrame:
        source = ctx.frame("daily")
        token = daily_snapshot_token(source)
        cached = self._daily_cache
        if cached is not None and cached[0] == token:
            return cached[1]
        daily = source.select(["symbol", "date", "close"]).sort(["symbol", "date"])
        daily = daily.with_columns(pl.col("close").shift(1).over("symbol").alias("_asof"))
        result = daily.with_columns(
            [(pl.col("_asof") / pl.col("_asof").shift(w).over("symbol") - 1.0).alias(f"_ret_{_tag(w, True)}") for w in DAY_WINDOWS]
        ).select(["symbol", "date", *[f"_ret_{_tag(w, True)}" for w in DAY_WINDOWS]])
        self._daily_cache = (token, result)
        return result

    def _market_by_minute(self, returns: pl.DataFrame, tags: list[str]) -> pl.DataFrame:
        """The GATHER: per minute, the std + IQR of the universe's returns over each tag (nulls auto-excluded
        by polars aggregates)."""
        aggs: list[pl.Expr] = []
        for tag in tags:
            col = pl.col(f"_ret_{tag}")
            aggs.append(col.std().alias(f"return_dispersion_std_{tag}"))
            aggs.append(
                (col.quantile(0.75) - col.quantile(0.25)).alias(f"return_dispersion_iqr_{tag}")
            )
        return returns.group_by("minute").agg(aggs)

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        """The GATHER + broadcast, emitted for the given output (symbol, minute) keys. The reduce ALWAYS runs
        over the FULL minute buffer (so the windowed ``lagged()`` returns are correct); only the OUTPUT keys
        are filtered — so compute_latest (latest-minute keys) == compute().last by construction."""
        names = [spec.name for spec in self.declare()]
        minute_tags = [_tag(w, False) for w in MINUTE_WINDOWS]
        day_tags = [_tag(w, True) for w in DAY_WINDOWS]

        minute_disp = self._market_by_minute(self._pin_universe(ctx, self._minute_returns(ctx)), minute_tags)

        daily_ret = self._pin_universe(ctx, self._daily_returns(ctx))
        daily_aggs: list[pl.Expr] = []
        for tag in day_tags:
            col = pl.col(f"_ret_{tag}")
            daily_aggs.append(col.std().alias(f"return_dispersion_std_{tag}"))
            daily_aggs.append((col.quantile(0.75) - col.quantile(0.25)).alias(f"return_dispersion_iqr_{tag}"))
        daily_disp = daily_ret.group_by("date").agg(daily_aggs)

        keys = out_keys.with_columns(pl.col("minute").dt.date().alias("date"))
        return (
            keys.join(minute_disp, on="minute", how="left")
            .join(daily_disp, on="date", how="left")
            .select(["symbol", "minute", *names])
        )

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE gather: the reduce runs over the FULL buffer (windowed returns intact); only the
        output keys are filtered to the latest minute, so compute_latest == compute().last (parity-guarded)."""
        keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = keys["minute"].max()
        return self._assemble(ctx, keys.filter(pl.col("minute") == latest))
