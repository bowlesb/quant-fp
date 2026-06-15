"""Market & sector breadth — what fraction of equities are moving up/down (family: CROSS_SECTIONAL).

A GATHER group, computed ONCE per minute, broadcast/joined to every ticker — the same per-minute
universe reduce as ``market_context``, not a per-symbol fold. For each horizon ``W`` and minute ``T``:

  * ``breadth_up_{W}``   = fraction of the universe whose return over ``W`` is up,
  * ``breadth_down_{W}`` = fraction whose return is down,
  * ``breadth_net_{W}``  = up − down,  all market-wide SCALARS broadcast to every ticker; and
  * ``sector_breadth_up_{W}`` / ``_down`` / ``_net`` = the SAME reduce grouped BY sector, joined onto
    each ticker by its own sector (from the reference snapshot).

THE PARITY TRICK — an aggregate of a discontinuous function. Breadth counts ``sign(return)``, and sign
jumps at 0: a return that differs by less than a cell tolerance between live and backfill (legit
float/tick-order noise) can still flip a symbol across zero and change the integer count, so cell
tolerance does NOT compose into the aggregate. The fix is a DEAD-BAND on the sign: a name is counted
``up`` only when ``ret_W > +EPS`` and ``down`` only when ``ret_W < -EPS``; a return within ``EPS`` of
zero is genuinely sign-ambiguous and lands FLAT — in the denominator (a valid return) but neither up
nor down. Excluding the ambiguous names makes the count robust to the noise that would flip them, so
breadth is parity-true by construction (proven by tests/test_fp_breadth.py, including a case with
returns sitting right on the zero boundary).

DENOMINATOR = symbols with a VALID return over ``W`` (close present at BOTH ``T`` and ``T-W``),
computed identically both sides. Pinned to the day's ``universe`` membership when provided so the
denominator cannot drift (the same pin market_context/parity use); null-sector tickers bucket to an
``UNKNOWN`` sector, never dropped. Intraday horizons use minute returns; 1d/5d use the daily frame
(inheriting its split-adjustment handling, same as the multi_day groups).
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

MINUTE_WINDOWS: tuple[int, ...] = (5, 30, 60)  # intraday horizons, in minutes
DAY_WINDOWS: tuple[int, ...] = (1, 5)  # multi-day horizons, in completed trading days
EPS: float = 1e-4  # dead-band half-width on the return sign (1 bp) — the parity trick
UNKNOWN_SECTOR: str = "unknown"  # bucket for null/unmapped sectors (never dropped)
SIDES: tuple[str, ...] = ("up", "down", "net")


def _window_tag(window: int, is_daily: bool) -> str:
    """The horizon suffix used in feature names: ``5m``/``30m``/``60m`` or ``1d``/``5d``."""
    return f"{window}d" if is_daily else f"{window}m"


def _up_down(return_col: str) -> tuple[pl.Expr, pl.Expr]:
    """The dead-band sign of a return column: (is_up, is_down) as 0/1 ints, FLAT inside ±EPS.

    A name with a null return is excluded from BOTH (not a valid return → not in the denominator);
    a name within ±EPS of zero is 0 for both (valid, in the denominator, but neither up nor down)."""
    return (
        (pl.col(return_col) > EPS).cast(pl.Float64),
        (pl.col(return_col) < -EPS).cast(pl.Float64),
    )


@register
class BreadthGroup(FeatureGroup):
    name = "breadth"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
        InputSpec(name="daily", columns=("symbol", "date", "close")),
        InputSpec(name="reference", columns=("symbol", "sector")),
    )

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for tag in self._all_tags():
            specs.append(
                FeatureSpec(
                    name=f"breadth_up_{tag}",
                    description=f"Fraction of the universe whose return over {tag} is up (> +1bp dead-band); a market-wide scalar broadcast to every ticker.",
                    dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"breadth_down_{tag}",
                    description=f"Fraction of the universe whose return over {tag} is down (< -1bp dead-band); a market-wide scalar broadcast to every ticker.",
                    dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"breadth_net_{tag}",
                    description=f"Net market breadth over {tag}: up fraction minus down fraction of the universe, broadcast to every ticker (positive = more up than down).",
                    dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"sector_breadth_up_{tag}",
                    description=f"Fraction of THIS ticker's sector whose return over {tag} is up (> +1bp dead-band), joined onto the ticker by its sector.",
                    dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"sector_breadth_down_{tag}",
                    description=f"Fraction of THIS ticker's sector whose return over {tag} is down (< -1bp dead-band), joined onto the ticker by its sector.",
                    dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"sector_breadth_net_{tag}",
                    description=f"Net breadth within THIS ticker's sector over {tag}: up fraction minus down fraction of the sector, joined onto the ticker by its sector.",
                    dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A",
                )
            )
        return specs

    def _all_tags(self) -> list[str]:
        return [_window_tag(w, False) for w in MINUTE_WINDOWS] + [_window_tag(w, True) for w in DAY_WINDOWS]

    def _sector_map(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-symbol normalized sector, with null/blank → the UNKNOWN bucket (never dropped)."""
        norm = (
            pl.col("sector").str.to_lowercase().str.replace_all(" ", "_")
        )
        return ctx.frame("reference").select(["symbol", "sector"]).with_columns(
            pl.when(pl.col("sector").is_null() | (pl.col("sector").str.strip_chars() == ""))
            .then(pl.lit(UNKNOWN_SECTOR))
            .otherwise(norm)
            .alias("_sector")
        ).select(["symbol", "_sector"])

    def _pin_universe(self, ctx: BatchContext, returns: pl.DataFrame) -> pl.DataFrame:
        """Pin the per-(symbol, minute, window) returns to the day's universe membership when provided,
        so the breadth denominator cannot drift between live and backfill. Without a universe frame the
        reduce runs over whatever printed (coverage-gated, like cross_sectional_rank)."""
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            return returns.join(members, on="symbol", how="inner")
        return returns

    def _minute_returns(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) trailing return over each MINUTE_WINDOWS horizon, as ``_ret_{w}m``
        columns. A cell is null where the bar exactly ``w`` minutes ago is absent (lagged() is
        time-based) — null = not a valid return, excluded from the denominator both sides."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for window in MINUTE_WINDOWS:
            frame = lagged(frame, "close", window, f"_lag{window}")
        frame = frame.sort(["symbol", "minute"])
        return frame.with_columns(
            [(pl.col("close") / pl.col(f"_lag{window}") - 1.0).alias(f"_ret_{_window_tag(window, False)}") for window in MINUTE_WINDOWS]
        ).select(["symbol", "minute", *[f"_ret_{_window_tag(w, False)}" for w in MINUTE_WINDOWS]])

    def _daily_returns(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, date) point-in-time daily return over each DAY_WINDOWS horizon, as
        ``_ret_{w}d`` columns. Point-in-time as of the PRIOR close (``_asof`` = close[D-1]), so at any
        minute of day D the most recent completed bar is used — identical to multi_day_returns; the
        daily frame is split-adjusted, so the long horizons inherit that handling."""
        daily = ctx.frame("daily").select(["symbol", "date", "close"]).sort(["symbol", "date"])
        daily = daily.with_columns(pl.col("close").shift(1).over("symbol").alias("_asof"))
        return daily.with_columns(
            [(pl.col("_asof") / pl.col("_asof").shift(window).over("symbol") - 1.0).alias(f"_ret_{_window_tag(window, True)}") for window in DAY_WINDOWS]
        ).select(["symbol", "date", *[f"_ret_{_window_tag(w, True)}" for w in DAY_WINDOWS]])

    def _reduce(self, returns: pl.DataFrame, sector_map: pl.DataFrame, tags: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
        """The GATHER: from per-(symbol, minute) returns, compute the market scalar per (minute, tag) and
        the sector scalar per (minute, sector, tag). Each ``up``/``down`` is a mean of the dead-band sign
        over names with a VALID return that minute (nulls auto-excluded by polars mean). Returns
        (market_by_minute, sector_by_minute_and_sector)."""
        with_sector = returns.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        market_aggs: list[pl.Expr] = []
        sector_aggs: list[pl.Expr] = []
        for tag in tags:
            is_up, is_down = _up_down(f"_ret_{tag}")
            market_aggs.append(is_up.mean().alias(f"breadth_up_{tag}"))
            market_aggs.append(is_down.mean().alias(f"breadth_down_{tag}"))
            sector_aggs.append(is_up.mean().alias(f"sector_breadth_up_{tag}"))
            sector_aggs.append(is_down.mean().alias(f"sector_breadth_down_{tag}"))
        market = with_sector.group_by("minute").agg(market_aggs)
        sector = with_sector.group_by(["minute", "_sector"]).agg(sector_aggs)
        market = market.with_columns(
            [(pl.col(f"breadth_up_{tag}") - pl.col(f"breadth_down_{tag}")).alias(f"breadth_net_{tag}") for tag in tags]
        )
        sector = sector.with_columns(
            [(pl.col(f"sector_breadth_up_{tag}") - pl.col(f"sector_breadth_down_{tag}")).alias(f"sector_breadth_net_{tag}") for tag in tags]
        )
        return market, sector

    def _assemble(self, ctx: BatchContext, minute_keys: pl.DataFrame) -> pl.DataFrame:
        """Compute the per-minute market + sector breadth reduce and broadcast/join it onto ``minute_keys``
        (the (symbol, minute) cells to emit). Shared by compute() and compute_latest() — only the set of
        minutes differs, which is what makes the live form parity-true with the backfill form."""
        tags = self._all_tags()
        sector_map = self._sector_map(ctx)

        minute_ret = self._minute_returns(ctx)
        daily_ret = self._daily_returns(ctx)
        # Broadcast the point-in-time daily returns onto each (symbol, minute) by trade date, exactly as
        # multi_day does, so the daily horizons reduce on the SAME minute grid as the intraday ones.
        minute_ret = minute_ret.with_columns(pl.col("minute").dt.date().alias("_date"))
        minute_ret = minute_ret.join(
            daily_ret, left_on=["symbol", "_date"], right_on=["symbol", "date"], how="left"
        ).drop("_date")

        returns = self._pin_universe(ctx, minute_ret)
        market, sector = self._reduce(returns, sector_map, tags)

        market_names = [f"breadth_{side}_{tag}" for tag in tags for side in SIDES]
        sector_names = [f"sector_breadth_{side}_{tag}" for tag in tags for side in SIDES]

        out = minute_keys.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        out = out.join(market, on="minute", how="left")
        out = out.join(sector, on=["minute", "_sector"], how="left")
        exprs = [pl.col(name).cast(pl.Float64).alias(name) for name in market_names + sector_names]
        names = [spec.name for spec in self.declare()]
        return out.with_columns(exprs).select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        return self._assemble(ctx, minute_keys)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE gather: the breadth at minute T depends only on THAT minute's per-symbol returns,
        so the reduce is identical to compute() — we just emit only the latest minute's rows. The minute
        returns are still built over the buffer (lagged join), then the reduce + broadcast run unchanged,
        so compute_latest == compute().last by construction (parity-guarded by tests/test_fp_latest)."""
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = minute_keys["minute"].max()
        return self._assemble(ctx, minute_keys.filter(pl.col("minute") == latest))
