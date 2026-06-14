"""Technical-indicator features from per-minute close (family: TECHNICAL, Layer A).

RSI, MACD, Bollinger, and SMA distances. Rolling stats are time-anchored; the EMAs (MACD) are
positional ewm over the minute grid (the standard for technical indicators) — same grid live &
backfill so parity holds.
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
from quantlib.features.latest import pivot_stat, rust_reductions, rust_windowed_sums
from quantlib.features.registry import register

SMA_WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 50, 100, 200)


@register
class TechnicalGroup(FeatureGroup):
    name = "technical"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TECHNICAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(name="rsi_14m", description="Relative Strength Index over the trailing 14 minutes (0-100).",
                        dtype="Float64", valid_range=(0.0, 100.0), nan_policy="warmup", layer="A"),
            FeatureSpec(name="macd_line", description="MACD line: 12-minute EMA minus 26-minute EMA of close.",
                        dtype="Float64", nan_policy="warmup", layer="A"),
            FeatureSpec(name="macd_signal", description="MACD signal line: 9-minute EMA of the MACD line.",
                        dtype="Float64", nan_policy="warmup", layer="A"),
            FeatureSpec(name="macd_hist", description="MACD histogram: MACD line minus the MACD signal line.",
                        dtype="Float64", nan_policy="warmup", layer="A"),
            FeatureSpec(name="bb_position_20m", description="Position of close within its 20-minute Bollinger band: (close - sma) / (2*std).",
                        dtype="Float64", nan_policy="warmup", layer="A"),
            FeatureSpec(name="bb_width_20m", description="Bollinger band width over 20 minutes: 4*std / sma (relative band width).",
                        dtype="Float64", valid_range=(0.0, None), nan_policy="warmup", layer="A"),
        ]
        for w in SMA_WINDOWS:
            specs.append(
                FeatureSpec(name=f"sma_dist_{w}m", description=f"Close relative to its trailing {w}-minute simple moving average (close/sma - 1).",
                            dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="warmup", layer="A")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        diff = pl.col("close") - pl.col("_prev")
        gain = pl.when(diff > 0).then(diff).otherwise(0.0)
        loss = pl.when(diff < 0).then(-diff).otherwise(0.0)
        avg_gain = gain.rolling_mean_by("minute", window_size="14m").over("symbol")
        avg_loss = loss.rolling_mean_by("minute", window_size="14m").over("symbol")
        ema12 = pl.col("close").ewm_mean(span=12).over("symbol")
        ema26 = pl.col("close").ewm_mean(span=26).over("symbol")
        sma20 = pl.col("close").rolling_mean_by("minute", window_size="20m").over("symbol")
        std20 = pl.col("close").rolling_std_by("minute", window_size="20m").over("symbol")
        frame = frame.with_columns(
            [
                (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)).cast(pl.Float64).alias("rsi_14m"),
                (ema12 - ema26).cast(pl.Float64).alias("macd_line"),
            ]
        )
        frame = frame.with_columns(pl.col("macd_line").ewm_mean(span=9).over("symbol").cast(pl.Float64).alias("macd_signal"))
        exprs = [
            (pl.col("macd_line") - pl.col("macd_signal")).cast(pl.Float64).alias("macd_hist"),
            ((pl.col("close") - sma20) / (2.0 * std20)).cast(pl.Float64).alias("bb_position_20m"),
            (4.0 * std20 / sma20).cast(pl.Float64).alias("bb_width_20m"),
        ]
        for w in SMA_WINDOWS:
            sma_w = pl.col("close").rolling_mean_by("minute", window_size=f"{w}m").over("symbol")
            exprs.append((pl.col("close") / sma_w - 1.0).cast(pl.Float64).alias(f"sma_dist_{w}m"))
        names = ["rsi_14m", "macd_line", "macd_signal", "macd_hist", "bb_position_20m", "bb_width_20m"] + [f"sma_dist_{w}m" for w in SMA_WINDOWS]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LATEST-MINUTE: RSI and the Bollinger/SMA stats become aggregate-at-T windowed reductions on the
        Rust kernels (RSI's avg-gain/avg-loss ratio = sum_gain/sum_loss, the per-window counts cancel); only
        MACD keeps the sequential EWM (taken at T). Same formulas as compute(), parity-guarded."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        latest = frame["minute"].max()
        diff = pl.col("close") - pl.col("_prev")
        frame = frame.with_columns(
            [
                pl.when(diff > 0).then(diff).otherwise(0.0).alias("_gain"),
                pl.when(diff < 0).then(-diff).otherwise(0.0).alias("_loss"),
                pl.col("close").ewm_mean(span=12).over("symbol").alias("_ema12"),
                pl.col("close").ewm_mean(span=26).over("symbol").alias("_ema26"),
            ]
        )
        frame = frame.with_columns((pl.col("_ema12") - pl.col("_ema26")).alias("_macd_line"))
        frame = frame.with_columns(pl.col("_macd_line").ewm_mean(span=9).over("symbol").alias("_macd_signal"))
        rsi = rust_windowed_sums(frame, ["_gain", "_loss"], (14,)).select(
            ["symbol", (100.0 - 100.0 / (1.0 + pl.col("_gain") / pl.col("_loss"))).cast(pl.Float64).alias("rsi_14m")]
        )
        red = rust_reductions(frame, "close", SMA_WINDOWS)  # 20 is in SMA_WINDOWS, so Bollinger reuses it
        means = pivot_stat(red, "mean", "_sma_{w}", SMA_WINDOWS)
        std20 = red.filter(pl.col("window") == 20).select(["symbol", pl.col("std").alias("_std20")])
        base = frame.filter(pl.col("minute") == latest).select(
            ["symbol", pl.col("close").alias("_cT"), "_macd_line", "_macd_signal"]
        )
        out = base.join(rsi, on="symbol", how="left").join(means, on="symbol", how="left").join(std20, on="symbol", how="left")
        exprs = [
            pl.col("_macd_line").cast(pl.Float64).alias("macd_line"),
            pl.col("_macd_signal").cast(pl.Float64).alias("macd_signal"),
            (pl.col("_macd_line") - pl.col("_macd_signal")).cast(pl.Float64).alias("macd_hist"),
            ((pl.col("_cT") - pl.col("_sma_20")) / (2.0 * pl.col("_std20"))).cast(pl.Float64).alias("bb_position_20m"),
            (4.0 * pl.col("_std20") / pl.col("_sma_20")).cast(pl.Float64).alias("bb_width_20m"),
        ]
        for w in SMA_WINDOWS:
            exprs.append((pl.col("_cT") / pl.col(f"_sma_{w}") - 1.0).cast(pl.Float64).alias(f"sma_dist_{w}m"))
        names = ["rsi_14m", "macd_line", "macd_signal", "macd_hist", "bb_position_20m", "bb_width_20m"] + [f"sma_dist_{w}m" for w in SMA_WINDOWS]
        return out.with_columns(exprs).with_columns(pl.lit(latest).alias("minute")).select(["symbol", "minute", *names])
