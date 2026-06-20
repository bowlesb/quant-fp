"""Per-GICS-sector trailing return + within-sector excess (family: CROSS_SECTIONAL, Layer A).

A universe-wide GATHER — computed ONCE per minute over EVERY symbol, grouped BY the symbol's GICS
sector, then broadcast/joined back onto each ticker by its own sector. The SAME per-minute universe
reduce as ``breadth``, but on RETURNS (equal-weight mean) instead of dead-band sign counts. For each
horizon ``W`` and minute ``T``:

  * ``sector_return_{W}`` = the equal-weight mean trailing-``W`` return of THIS ticker's sector
    (every name in the sector gets its sector's aggregate), and
  * ``sector_excess_{W}`` = this ticker's own trailing-``W`` return minus its sector aggregate (the
    within-sector excess — how much the name out/under-performs its peers over ``W``).

UNKNOWN SECTOR → NULL. A symbol with no mapped GICS sector (``sector_is_unknown`` — the ~27% unmapped)
has no peer group to aggregate, so both features are NULL for it (NOT bucketed into a synthetic
"unknown sector" aggregate the way ``breadth`` counts them — an unknown-sector mean is not a sector
return). The denominator for each sector aggregate is the symbols in THAT sector with a VALID return
over ``W`` (close present at both ``T`` and ``T-W``), computed identically live and in backfill.

PARITY — parity-true by construction. The sector aggregate is a deterministic per-(minute, sector)
equal-weight mean of the same minute-bar returns both sides; pinned to the day's ``universe`` membership
when provided so the per-sector denominator cannot drift (the same pin breadth/market_context use).
Mean of returns is a CONTINUOUS reduce (no sign discontinuity, unlike breadth's count), so cell
tolerance composes — no dead-band needed. ``compute_latest`` emits only T's rows from the identical
reduce, so live == backfill (parity-guarded by tests/test_fp_latest + tests/test_fp_sector_return).
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

MINUTE_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)  # intraday horizons, in minutes
UNKNOWN_SECTOR: str = "unknown"  # the bucket null/unmapped sectors land in (then dropped from output)


def _tag(window: int) -> str:
    return f"{window}m"


@register
class SectorReturnGroup(FeatureGroup):
    name = "sector_return"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
        InputSpec(name="reference", columns=("symbol", "sector")),
    )

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for window in MINUTE_WINDOWS:
            tag = _tag(window)
            specs.append(
                FeatureSpec(
                    name=f"sector_return_{tag}",
                    description=f"Equal-weight mean trailing {tag} return of THIS ticker's GICS sector, joined onto the ticker by its sector (NULL for unmapped-sector names).",
                    dtype="Float64",
                    valid_range=(-1.0, 5.0),
                    nan_policy="sparse",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"sector_excess_{tag}",
                    description=f"This ticker's trailing {tag} return minus its sector's equal-weight mean over the same window (within-sector excess; NULL for unmapped-sector names).",
                    dtype="Float64",
                    valid_range=(-6.0, 6.0),
                    nan_policy="sparse",
                    layer="A",
                )
            )
        return specs

    def reduce_buffer_minutes(self) -> int | None:
        """A universe-wide GATHER (runs in the reader's reduce phase, not per shard), so the reader's
        minimal reduce ring must be deep enough for the longest trailing horizon."""
        return max(MINUTE_WINDOWS)

    def _sector_map(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-symbol normalized sector, null/blank → the UNKNOWN bucket (so the join is total; the
        unknown bucket is excluded from the OUTPUT below, never silently joined as a real sector)."""
        norm = pl.col("sector").str.to_lowercase().str.replace_all(" ", "_")
        return (
            ctx.frame("reference")
            .select(["symbol", "sector"])
            .with_columns(
                pl.when(pl.col("sector").is_null() | (pl.col("sector").str.strip_chars() == ""))
                .then(pl.lit(UNKNOWN_SECTOR))
                .otherwise(norm)
                .alias("_sector")
            )
            .select(["symbol", "_sector"])
        )

    def _pin_universe(self, ctx: BatchContext, returns: pl.DataFrame) -> pl.DataFrame:
        """Pin the per-(symbol, minute) returns to the day's universe membership when provided, so the
        per-sector aggregate denominator cannot drift between live and backfill (the breadth pin)."""
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            return returns.join(members, on="symbol", how="inner")
        return returns

    def _minute_returns(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) trailing return over each horizon as ``_ret_{w}m``. A cell is null where
        the bar exactly ``w`` minutes ago is absent (time-based lag) — null = not a valid return,
        excluded from the sector mean both sides."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for window in MINUTE_WINDOWS:
            frame = lagged(frame, "close", window, f"_lag{window}")
        frame = frame.sort(["symbol", "minute"])
        return frame.with_columns(
            [
                (pl.col("close") / pl.col(f"_lag{window}") - 1.0).alias(f"_ret_{_tag(window)}")
                for window in MINUTE_WINDOWS
            ]
        ).select(["symbol", "minute", *[f"_ret_{_tag(w)}" for w in MINUTE_WINDOWS]])

    def _reduce(self, returns: pl.DataFrame, sector_map: pl.DataFrame) -> pl.DataFrame:
        """The GATHER: per-(minute, sector) equal-weight mean trailing return. Null returns are
        auto-excluded by polars mean; the UNKNOWN bucket is kept here (it is dropped at broadcast time)."""
        with_sector = returns.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        aggs = [pl.col(f"_ret_{_tag(w)}").mean().alias(f"sector_return_{_tag(w)}") for w in MINUTE_WINDOWS]
        return with_sector.group_by(["minute", "_sector"]).agg(aggs)

    def _assemble(self, ctx: BatchContext, minute_keys: pl.DataFrame) -> pl.DataFrame:
        """Compute the per-(minute, sector) aggregate and broadcast it onto ``minute_keys`` by sector,
        then derive the within-sector excess from the symbol's own trailing return. Shared by compute()
        and compute_latest() — only the set of minutes differs, which is what makes the live form
        parity-true with the backfill form."""
        sector_map = self._sector_map(ctx)
        minute_ret = self._minute_returns(ctx)
        returns = self._pin_universe(ctx, minute_ret)
        sector = self._reduce(returns, sector_map)

        # Each cell carries its OWN trailing return (for the excess) + its sector tag (for the join).
        out = minute_keys.join(minute_ret, on=["symbol", "minute"], how="left")
        out = out.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        out = out.join(sector, on=["minute", "_sector"], how="left")

        exprs: list[pl.Expr] = []
        is_unknown = pl.col("_sector") == UNKNOWN_SECTOR
        for window in MINUTE_WINDOWS:
            tag = _tag(window)
            sret = pl.when(is_unknown).then(None).otherwise(pl.col(f"sector_return_{tag}"))
            excess = (
                pl.when(is_unknown)
                .then(None)
                .otherwise(pl.col(f"_ret_{tag}") - pl.col(f"sector_return_{tag}"))
            )
            exprs.append(sret.cast(pl.Float64).alias(f"sector_return_{tag}"))
            exprs.append(excess.cast(pl.Float64).alias(f"sector_excess_{tag}"))
        names = [spec.name for spec in self.declare()]
        return out.with_columns(exprs).select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        return self._assemble(ctx, minute_keys)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE gather: the sector aggregate at minute T depends only on THAT minute's per-symbol
        returns, so the reduce is identical to compute() — we emit only the latest minute's rows. The
        minute returns are still built over the buffer (lagged join), then the reduce + broadcast run
        unchanged, so compute_latest == compute().last by construction (tests/test_fp_latest)."""
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = minute_keys["minute"].max()
        return self._assemble(ctx, minute_keys.filter(pl.col("minute") == latest))
