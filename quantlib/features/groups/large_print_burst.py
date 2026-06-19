"""Large-print burst features from the raw tape (family: MICROSTRUCTURE, Layer C).

WHY (vol-burst finding, experiments/2026-06-19-volburst): an unusually large trade print relative to the
name's OWN recent trade-size scale is a bar-clearing driver of an imminent large move — in the walk-forward
burst classifier (OOS ROC-AUC up to 0.92 for the |forward-return| >= 2% label) the large-print burst term
carries real univariate signal alongside ``rv3`` and inter-arrival burstiness. The existing
``trade_size_dist.institutional_trade_ratio_1m`` fires only on an ABSOLUTE 10,000-share block; it never sees
a print that is large FOR THIS NAME (a 2,000-share print on a thin stock that normally trades 100-lots is a
burst there, invisible to a fixed 10k cutoff). This group fills that gap: prints large relative to the
minute's own mean trade size.

PARITY (Layer C, PARITY_PROMOTION_GATE.md): the research screen used a DAY-LEVEL 90th-percentile size
threshold — that is look-ahead within the day (the threshold peeks at the whole session) and has no bounded
incremental twin, so it cannot reproduce live. We re-express it parity-true: each (symbol, minute) cell is a
pure function of THAT MINUTE'S tape only — the threshold is ``LARGE_PRINT_MULT`` times the minute's own mean
print size — exactly the own-minute-only shape ``trade_size_dist`` already ships (so the default
``compute_latest`` = ``compute().last`` is parity-true by construction; the look-ahead guard is trivially
satisfied — a cell reads only its own minute). The SAME code runs live and on backfill, so live == backfill;
the two differ only in the ticks each was fed, which the parity audit measures.

GUARDS: every ratio guards its denominator (Guard 2 — ``mean_size > 0`` / ``n_trades > 0`` → NULL, never a
raw ``num/denom`` that can divide by zero on an empty/degenerate minute) and a final ``is_finite()`` backstop
converts any stray non-finite to the agreed NULL identically. RT-GREEN (one bounded group-by over the
minute's ticks, the ~2.5ms Layer-C floor; no window, no OLS, no order statistic). A tradeless minute yields
no row (the honest "no trades", not a fabricated zero).
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

# A print at/above this many times the MINUTE'S OWN mean trade size is "large for this name this minute".
LARGE_PRINT_MULT = 4.0

_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "large_print_ratio_1m": pl.Float64,
    "large_print_volume_share_1m": pl.Float64,
    "max_print_size_ratio_1m": pl.Float64,
}


@register
class LargePrintBurstGroup(FeatureGroup):
    name = "large_print_burst"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="large_print_ratio_1m",
                description=(
                    "Fraction of this minute's trades whose size is at least 4x the minute's own mean print "
                    "size — a relative-to-own-scale large-print burst (vol-burst study, OOS ROC-AUC up to "
                    "0.92). Count-weighted, in [0, 1]; null on a tradeless or zero-mean-size minute."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="large_print_volume_share_1m",
                description=(
                    "Share of this minute's traded volume that printed in large prints (size >= 4x the "
                    "minute's mean print size) — how concentrated the minute's volume is in outsized prints. "
                    "Volume-weighted, in [0, 1]; null on a tradeless or zero-volume minute."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="max_print_size_ratio_1m",
                description=(
                    "Largest single print this minute relative to the minute's mean print size "
                    "(max size / mean size) — the peak outlier print, >= 1. Null on a tradeless or "
                    "zero-mean-size minute."
                ),
                dtype="Float64",
                valid_range=(1.0, None),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts", "size"])
        if trades.height == 0:
            return pl.DataFrame(schema=_SCHEMA)
        # Each cell is a pure function of its own minute's tape: bucket by the EXCHANGE timestamp, then per
        # (symbol, minute) compute the mean print size and the large-print threshold from THAT minute only —
        # no window, no cross-minute state — so live (compute_latest = this filtered to the last minute) and
        # backfill agree by construction.
        per_trade = trades.with_columns(pl.col("ts").dt.truncate("1m").alias("minute")).with_columns(
            pl.col("size").mean().over(["symbol", "minute"]).alias("_mean_size"),
        )
        threshold = LARGE_PRINT_MULT * pl.col("_mean_size")
        per_trade = per_trade.with_columns(
            (pl.col("size") >= threshold).alias("_is_large"),
        )
        agg = per_trade.group_by(["symbol", "minute"]).agg(
            pl.col("_is_large").mean().cast(pl.Float64).alias("_large_count_share"),
            pl.col("size").filter(pl.col("_is_large")).sum().cast(pl.Float64).alias("_large_vol"),
            pl.col("size").sum().cast(pl.Float64).alias("_total_vol"),
            pl.col("size").max().cast(pl.Float64).alias("_max_size"),
            pl.col("_mean_size").first().cast(pl.Float64).alias("_mean_size"),
        )
        # Guard every denominator (Guard 2: denom > 0 → NULL, never raw num/denom), then an is_finite()
        # backstop that converts any stray non-finite to the agreed NULL — identical on both paths.
        large_ratio = pl.col("_large_count_share")  # already a guarded mean over the minute's trades
        vol_share = (
            pl.when(pl.col("_total_vol") > 0.0)
            .then(pl.col("_large_vol").fill_null(0.0) / pl.col("_total_vol"))
            .otherwise(None)
        )
        max_ratio = (
            pl.when(pl.col("_mean_size") > 0.0)
            .then(pl.col("_max_size") / pl.col("_mean_size"))
            .otherwise(None)
        )
        return agg.with_columns(
            large_print_ratio_1m=_finite(large_ratio),
            large_print_volume_share_1m=_finite(vol_share),
            max_print_size_ratio_1m=_finite(max_ratio),
        ).select(
            [
                "symbol",
                "minute",
                "large_print_ratio_1m",
                "large_print_volume_share_1m",
                "max_print_size_ratio_1m",
            ]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Own-minute-only live path: every cell reads ONLY its own minute's tape, so the SAME ``compute()`` on
        the trailing 1-minute tape slice (filtered to T) is parity-true by construction — older trades cannot
        affect T's value. Avoids running the per-minute group-by over the whole ~300m trade buffer."""
        return self.compute_latest_on_window(ctx, 1)


def _finite(expr: pl.Expr) -> pl.Expr:
    """is_finite() backstop: any inf/-inf/nan that slips through becomes NULL identically on both paths."""
    return pl.when(expr.is_finite()).then(expr).otherwise(pl.lit(None, dtype=pl.Float64))
