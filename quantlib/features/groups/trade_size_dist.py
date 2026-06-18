"""Trade-size distribution features from the raw tape (family: MICROSTRUCTURE, Layer C).

Per-minute fractions of trade COUNT by print size — the classic retail-vs-institutional flow proxies:
odd lots (< 100 shares, dominated by retail since the 2020 fractional-share era), round lots (an exact
multiple of 100, the historical institutional/algos default), and large/institutional prints
(>= 10,000 shares). These are pure per-minute fractions of that minute's trades, so each (symbol, minute)
cell depends ONLY on the trades printed in that minute — no window, no cross-minute state. That makes the
default ``compute_latest`` (``compute().filter(last minute)``) already parity-true, and the look-ahead
guard trivially satisfied (a cell reads only its own minute's tape).

Like ``tick_runlength`` / ``microstructure_burst`` this is a Layer-C ``trades``-frame group: the SAME code
runs on the live tape and the historical backfill, so live == backfill by construction; live and backfill
differ only in the ticks they were fed, which the parity audit measures. A tradeless minute yields no row
(the honest "no trades", not a fabricated zero).
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

ODD_LOT_MAX = 100.0  # a print strictly below this many shares is an odd lot
INSTITUTIONAL_MIN = 10_000.0  # a print at/above this many shares is a large/institutional print


@register
class TradeSizeDistGroup(FeatureGroup):
    name = "trade_size_dist"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="odd_lot_ratio_1m",
                description=(
                    "Fraction of this minute's trades that are odd lots (size < 100 shares) — a retail-flow "
                    "proxy. Count-weighted, in [0, 1]; null on a tradeless minute."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="round_lot_ratio_1m",
                description=(
                    "Fraction of this minute's trades whose size is an exact multiple of 100 shares (a round "
                    "lot) — an institutional/algo-default proxy. Count-weighted, in [0, 1]; null when tradeless."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="institutional_trade_ratio_1m",
                description=(
                    "Fraction of this minute's trades that are large prints (size >= 10,000 shares) — a "
                    "block/institutional-flow proxy. Count-weighted, in [0, 1]; null on a tradeless minute."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts", "size"])
        if trades.height == 0:
            return pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "minute": pl.Datetime("us", "UTC"),
                    "odd_lot_ratio_1m": pl.Float64,
                    "round_lot_ratio_1m": pl.Float64,
                    "institutional_trade_ratio_1m": pl.Float64,
                }
            )
        return (
            trades.with_columns(pl.col("ts").dt.truncate("1m").alias("minute"))
            .with_columns(
                (pl.col("size") < ODD_LOT_MAX).alias("_is_odd"),
                # Round lot = an exact multiple of 100 shares (and a positive print).
                ((pl.col("size") > 0) & (pl.col("size") % 100.0 == 0.0)).alias("_is_round"),
                (pl.col("size") >= INSTITUTIONAL_MIN).alias("_is_inst"),
            )
            .group_by(["symbol", "minute"])
            .agg(
                pl.col("_is_odd").mean().cast(pl.Float64).alias("odd_lot_ratio_1m"),
                pl.col("_is_round").mean().cast(pl.Float64).alias("round_lot_ratio_1m"),
                pl.col("_is_inst").mean().cast(pl.Float64).alias("institutional_trade_ratio_1m"),
            )
            .select(
                [
                    "symbol",
                    "minute",
                    "odd_lot_ratio_1m",
                    "round_lot_ratio_1m",
                    "institutional_trade_ratio_1m",
                ]
            )
        )
