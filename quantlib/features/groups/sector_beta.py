"""Ticker→sector alignment: rolling beta/correlation of a ticker to its OWN GICS sector (family:
CROSS_SECTIONAL, Layer A).

How closely a ticker tracks its sector — the sector analogue of ``market_beta`` (which regresses on
SPY), but the regressor is the ticker's OWN sector's equal-weight one-minute return, not the index.
Per symbol, regress its one-minute return (``y``) on its sector's one-minute aggregate return (``x``)
over each trailing window: the slope is the rolling sector beta, the correlation is how tightly the
name co-moves with its sector.

  * ``sector_beta_{W}`` = slope of the windowed OLS of the ticker's one-minute return on its sector's,
  * ``sector_corr_{W}`` = the correlation of the two over the same window, in [-1, 1].

OFF THE INCREMENTAL FAST-PATH BY DESIGN. The sector aggregate is a per-minute universe gather (every
symbol in the sector), so this is a universe-wide GATHER like ``sector_return`` (a REDUCE_GROUP). It is
a PLAIN ``FeatureGroup`` — the windowed OLS is computed by polars time-based rolling power sums
(``rolling_sum_by`` over minute), NOT a ``ReductionGroup``/``StatefulRegressor``. A correlation/OLS
feature on a gappy minute grid breaches the incremental self-check denominator (the #180 lesson:
``market_beta`` and friends are gated OFF FP_INCREMENTAL), so this group deliberately never rides the
incremental path — the rolling form is the single source of truth, live and backfill.

UNKNOWN SECTOR → NULL. A symbol with no mapped GICS sector has no sector series to regress on, so both
features are NULL for it (the ~27% unmapped). A near-degenerate fit (n below the minimum pairs, or
zero sector-return variance over the window — a thin/after-hours window) → NULL rather than a
non-physical beta.

PARITY — parity-true by construction. The sector aggregate is the SAME deterministic per-(minute,
sector) equal-weight mean both sides (identical to ``sector_return``), pinned to the day's ``universe``
when provided; the rolling OLS over the resulting paired (own, sector) one-minute returns is the same
backfill rolling form the window-sliced ``compute_latest`` runs (tests/test_fp_latest +
tests/test_fp_sector_beta).
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (15, 30, 60)  # OLS windows, in minutes
MIN_PAIRS = 5  # fewer than this many paired one-minute returns over the window -> undefined fit
BETA_TOL = 1e-4
# |beta| beyond this is a degenerate near-zero-variance OLS fit (thin window where the sector barely
# moves: cov / tiny-denom explodes). Null it rather than ship a non-physical beta — same guard as market_beta.
BETA_MAX = 15.0
UNKNOWN_SECTOR: str = "unknown"
WARMUP_SLACK = (
    2  # extra minutes for compute_latest's window slice (the 1-bar lag a return needs at the edge)
)


def _tag(window: int) -> str:
    return f"{window}m"


@register
class SectorBetaGroup(FeatureGroup):
    name = "sector_beta"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
        InputSpec(name="reference", columns=("symbol", "sector")),
    )

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for window in WINDOWS:
            tag = _tag(window)
            specs.append(
                FeatureSpec(
                    name=f"sector_beta_{tag}",
                    description=f"Rolling beta to THIS ticker's GICS sector over {tag}: slope of its one-minute return regressed on its sector's equal-weight one-minute return (NULL for unmapped-sector names).",
                    dtype="Float64",
                    valid_range=(-15.0, 15.0),
                    nan_policy="sparse",
                    layer="A",
                    tolerance=BETA_TOL,
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"sector_corr_{tag}",
                    description=f"Rolling correlation of this ticker's one-minute return with its GICS sector's over {tag}, in [-1, 1] (NULL for unmapped-sector names).",
                    dtype="Float64",
                    valid_range=(-1.01, 1.01),
                    nan_policy="sparse",
                    layer="A",
                    tolerance=BETA_TOL,
                )
            )
        return specs

    def reduce_buffer_minutes(self) -> int | None:
        """A universe-wide GATHER (the sector aggregate needs every symbol), so the reader's minimal reduce
        ring must cover the deepest OLS window plus the 1-bar warmup the one-minute return needs at the edge.
        """
        return max(WINDOWS) + WARMUP_SLACK

    def _sector_map(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-symbol normalized sector, null/blank → the UNKNOWN bucket (total join; unknown is NULLed in output)."""
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

    def _pin_universe(self, ctx: BatchContext, frame: pl.DataFrame) -> pl.DataFrame:
        """Pin to the day's universe membership when provided so the per-sector aggregate denominator
        cannot drift between live and backfill (the breadth/sector_return pin)."""
        if "universe" in ctx.frames:
            members = ctx.frames["universe"].select("symbol").unique()
            return frame.join(members, on="symbol", how="inner")
        return frame

    def _own_minute_return(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) one-minute return ``_oret`` = close[T]/close[T-1m] - 1, time-based (null
        across a gap, never a multi-minute jump treated as one minute)."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        prior = frame.select(
            pl.col("symbol"),
            (pl.col("minute") + pl.duration(minutes=1)).alias("minute"),
            pl.col("close").alias("_prev_close"),
        )
        frame = frame.join(prior, on=["symbol", "minute"], how="left")
        return frame.with_columns((pl.col("close") / pl.col("_prev_close") - 1.0).alias("_oret")).select(
            ["symbol", "minute", "_oret"]
        )

    def _sector_minute_return(self, own: pl.DataFrame, sector_map: pl.DataFrame) -> pl.DataFrame:
        """Per-(minute, sector) equal-weight mean one-minute return ``_sret`` — the regressor series.
        Same construction as sector_return's aggregate, on one-minute returns."""
        with_sector = own.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        return with_sector.group_by(["minute", "_sector"]).agg(pl.col("_oret").mean().alias("_sret"))

    def _assemble(self, ctx: BatchContext, minute_keys: pl.DataFrame) -> pl.DataFrame:
        """Build the paired (own, sector) one-minute returns, run the rolling OLS, emit on ``minute_keys``.
        Shared by compute() and compute_latest()."""
        paired = self._paired(ctx)
        exprs: list[pl.Expr] = []
        is_unknown = pl.col("_sector") == UNKNOWN_SECTOR
        for window in WINDOWS:
            beta, corr = self._ols(window)
            tag = _tag(window)
            exprs.append(
                pl.when(is_unknown).then(None).otherwise(beta).cast(pl.Float64).alias(f"sector_beta_{tag}")
            )
            exprs.append(
                pl.when(is_unknown).then(None).otherwise(corr).cast(pl.Float64).alias(f"sector_corr_{tag}")
            )

        out = paired.with_columns(exprs)
        names = [spec.name for spec in self.declare()]
        out = out.join(minute_keys, on=["symbol", "minute"], how="right")
        return out.select(["symbol", "minute", *names])

    def _paired(self, ctx: BatchContext) -> pl.DataFrame:
        """Per-(symbol, minute) paired (own, sector) one-minute returns + the 6 OLS power-sum building blocks
        (``__one/__x/__y/__xx/__yy/__xy``), zeroed on any minute missing either side so all sums share one
        denominator. Shared by ``_assemble`` (rolling) and ``_assemble_latest`` (window-slice at T)."""
        sector_map = self._sector_map(ctx)
        own = self._own_minute_return(ctx)
        own_pinned = self._pin_universe(ctx, own)
        sector = self._sector_minute_return(own_pinned, sector_map)

        paired = own.join(sector_map, on="symbol", how="left").with_columns(
            pl.col("_sector").fill_null(UNKNOWN_SECTOR)
        )
        paired = paired.join(sector, on=["minute", "_sector"], how="left").sort(["symbol", "minute"])
        both = pl.col("_oret").is_not_null() & pl.col("_sret").is_not_null()
        return paired.with_columns(
            pl.when(both).then(1.0).otherwise(0.0).alias("__one"),
            pl.when(both).then(pl.col("_sret")).otherwise(0.0).alias("__x"),
            pl.when(both).then(pl.col("_oret")).otherwise(0.0).alias("__y"),
            pl.when(both).then(pl.col("_sret") * pl.col("_sret")).otherwise(0.0).alias("__xx"),
            pl.when(both).then(pl.col("_oret") * pl.col("_oret")).otherwise(0.0).alias("__yy"),
            pl.when(both).then(pl.col("_sret") * pl.col("_oret")).otherwise(0.0).alias("__xy"),
        )

    def _assemble_latest(self, ctx: BatchContext, latest: object) -> pl.DataFrame:
        """The OLS at the LATEST minute T ONLY — the live-path fast form. ``_assemble`` runs the rolling
        power sums (5 windows x 6 ``rolling_sum_by`` over the WHOLE buffer) then keeps only T; each window's
        OLS at T reads only the trailing ``(T-w, T]`` paired returns, so SUM each window's slice directly
        (a minute-cutoff filter + per-symbol group, NOT a rolling) and run the IDENTICAL ``_ols_from_sums``
        beta/corr. Value-identical to ``_assemble(...).filter(==T)`` by construction (same paired rows, same
        time-based window, same power-sum algebra)."""
        paired = self._paired(ctx)
        base = (
            paired.filter(pl.col("minute") == latest)
            .select(["symbol", "minute", "_sector"])
            .sort("symbol")
        )
        is_unknown = pl.col("_sector") == UNKNOWN_SECTOR
        for window in WINDOWS:
            tag = _tag(window)
            sums = (
                paired.filter(
                    (pl.col("minute") > latest - pl.duration(minutes=window)) & (pl.col("minute") <= latest)
                )
                .group_by("symbol", maintain_order=True)
                .agg(
                    pl.col("__one").sum().alias("__n"),
                    pl.col("__x").sum().alias("__sx"),
                    pl.col("__y").sum().alias("__sy"),
                    pl.col("__xx").sum().alias("__sxx"),
                    pl.col("__yy").sum().alias("__syy"),
                    pl.col("__xy").sum().alias("__sxy"),
                )
            )
            beta_raw, corr_raw = self._ols_from_sums(
                pl.col("__n"), pl.col("__sx"), pl.col("__sy"), pl.col("__sxx"), pl.col("__syy"), pl.col("__sxy")
            )
            piece = sums.select(
                "symbol",
                beta_raw.cast(pl.Float64).alias(f"sector_beta_{tag}"),
                corr_raw.cast(pl.Float64).alias(f"sector_corr_{tag}"),
            )
            base = base.join(piece, on="symbol", how="left")
        # NULL the unknown bucket exactly as _assemble does (sector tag carries no sector OLS).
        nulls = []
        for window in WINDOWS:
            tag = _tag(window)
            for stat in ("beta", "corr"):
                col = f"sector_{stat}_{tag}"
                nulls.append(pl.when(is_unknown).then(None).otherwise(pl.col(col)).alias(col))
        base = base.with_columns(nulls)
        names = [spec.name for spec in self.declare()]
        return base.select(["symbol", "minute", *names])

    def _ols(self, window: int) -> tuple[pl.Expr, pl.Expr]:
        """Rolling windowed OLS of own return (y) on sector return (x) over the trailing ``window`` minutes,
        from time-based rolling power sums. Returns (beta, corr). Undefined cells (n < MIN_PAIRS, zero
        x-variance, or |beta| > BETA_MAX) -> null."""
        size = f"{window}m"

        def roll(name: str) -> pl.Expr:
            return pl.col(name).rolling_sum_by("minute", window_size=size).over("symbol")

        return self._ols_from_sums(
            roll("__one"), roll("__x"), roll("__y"), roll("__xx"), roll("__yy"), roll("__xy")
        )

    @staticmethod
    def _ols_from_sums(
        n: pl.Expr, sx: pl.Expr, sy: pl.Expr, sxx: pl.Expr, syy: pl.Expr, sxy: pl.Expr
    ) -> tuple[pl.Expr, pl.Expr]:
        """OLS beta/corr of y-on-x from the six power sums — the SINGLE source of truth shared by the rolling
        backfill path (``_ols`` over ``rolling_sum_by`` sums) and the live latest-only path
        (``_assemble_latest`` over per-window slice sums). Undefined cells (n < MIN_PAIRS, zero x/y-variance,
        |beta| > BETA_MAX) -> null."""
        cov = sxy - sx * sy / n
        var_x = sxx - sx * sx / n
        var_y = syy - sy * sy / n
        beta_raw = cov / var_x
        corr_raw = cov / (var_x.sqrt() * var_y.sqrt())
        defined = (n >= MIN_PAIRS) & (var_x > 0.0) & (var_y > 0.0)
        beta = pl.when(defined & (beta_raw.abs() <= BETA_MAX)).then(beta_raw).otherwise(None)
        corr = pl.when(defined).then(corr_raw.clip(-1.0, 1.0)).otherwise(None)
        return beta, corr

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        minute_keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        return self._assemble(ctx, minute_keys)

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE gather. The OLS at T reads only the trailing ``window`` minutes of paired returns,
        so running the identical rolling form over the whole buffer and emitting T's row is parity-true; the
        window slice is left to compute_latest_on_window callers. Here we emit T's rows from the same
        _assemble (parity-guarded == compute().last by tests/test_fp_latest)."""
        latest = ctx.frame("minute_agg")["minute"].max()
        return self._assemble_latest(ctx, latest)
