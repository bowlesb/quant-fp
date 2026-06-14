"""Technical-indicator features from per-minute close (family: TECHNICAL, Layer A).

RSI, MACD, Bollinger, and SMA distances. Two state KINDS (docs/STATE_ABSTRACTION.md):
  * the SMA/RSI/Bollinger stats are time-anchored WINDOWED REDUCTIONS — aggregate-at-T on the Rust kernels;
  * MACD is RECURSIVE: 12/26-span EMAs of close and a 9-span EMA of the macd line. These are maintained as
    ``EMAState`` (one running adjusted-EWM ``(num, den)`` per (symbol, span)) and folded one minute at a time
    on the live path; the backfill reaches the identical EMAs with polars ``ewm_mean``. Same grid + same
    recursion live and backfill, so MACD parity holds by construction.

``compute``/``compute_latest`` build the SAME state frame (close + the three EMAs + the windowed-reduction
columns) and evaluate the SAME ``assemble``; the live fast path (StatefulEngine) folds the EMAs and supplies
the reductions via ``reduction_columns`` — guarded == compute_latest by tests/test_fp_stateful.py.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureSpec,
    FeatureType,
    InputSpec,
    lagged,
)
from quantlib.features.latest import pivot_stat, rust_reductions, rust_windowed_sums
from quantlib.features.registry import register
from quantlib.features.stateful import EMASpec, StatefulGroup

SMA_WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 50, 100, 200)


def _macd_line(emitted: dict[str, np.ndarray], sources: dict[str, np.ndarray]) -> np.ndarray:
    """The macd-line series the 9-span signal EMA folds: ema12 − ema26 at this minute (the same per-minute
    expression the backfill ewms via ``rolling=pl.col("_ema12") - pl.col("_ema26")``)."""
    return emitted["_ema12"] - emitted["_ema26"]


@register
class TechnicalGroup(StatefulGroup):
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

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """At-T columns the state frame carries: close (the EMA source + the at-T price the reductions use)."""
        return frame

    def ema_specs(self) -> list[EMASpec]:
        return [
            EMASpec(alias="_ema12", span=12, source="close"),
            EMASpec(alias="_ema26", span=26, source="close"),
            EMASpec(alias="_macd_signal", span=9, combine=_macd_line,
                    rolling=pl.col("_ema12") - pl.col("_ema26")),
        ]

    def assemble(self) -> dict[str, pl.Expr]:
        """MACD from the three EMA columns + RSI/Bollinger/SMA from the reduction columns the state frame
        carries (``_rsi_14m``, ``_std20``, ``_sma_<w>``, with ``close`` as the at-T price)."""
        macd_line = pl.col("_ema12") - pl.col("_ema26")
        feats: dict[str, pl.Expr] = {
            "rsi_14m": pl.col("_rsi_14m"),
            "macd_line": macd_line,
            "macd_signal": pl.col("_macd_signal"),
            "macd_hist": macd_line - pl.col("_macd_signal"),
            "bb_position_20m": (pl.col("close") - pl.col("_sma_20")) / (2.0 * pl.col("_std20")),
            "bb_width_20m": 4.0 * pl.col("_std20") / pl.col("_sma_20"),
        }
        for w in SMA_WINDOWS:
            feats[f"sma_dist_{w}m"] = pl.col("close") / pl.col(f"_sma_{w}") - 1.0
        return feats

    def reduction_columns(self, ctx: BatchContext) -> pl.DataFrame:
        """The windowed-reduction columns (one row per symbol at T): RSI's avg-gain/avg-loss ratio (= the
        per-window gain/loss sums, counts cancel), each SMA mean, and the 20m std for Bollinger — on the Rust
        kernels (the SAME aggregate-at-T form, parity-guarded). Joined onto the EMA state frame by the live
        StatefulEngine and by compute_latest."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        diff = pl.col("close") - pl.col("_prev")
        frame = frame.with_columns(
            [
                pl.when(diff > 0).then(diff).otherwise(0.0).alias("_gain"),
                pl.when(diff < 0).then(-diff).otherwise(0.0).alias("_loss"),
            ]
        )
        rsi = rust_windowed_sums(frame, ["_gain", "_loss"], (14,)).select(
            ["symbol", (100.0 - 100.0 / (1.0 + pl.col("_gain") / pl.col("_loss"))).cast(pl.Float64).alias("_rsi_14m")]
        )
        red = rust_reductions(frame, "close", SMA_WINDOWS)  # 20 is in SMA_WINDOWS, so Bollinger reuses it
        means = pivot_stat(red, "mean", "_sma_{w}", SMA_WINDOWS)
        std20 = red.filter(pl.col("window") == 20).select(["symbol", pl.col("std").alias("_std20")])
        return rsi.join(means, on="symbol", how="left").join(std20, on="symbol", how="left")

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """LIVE form: EMAs via the sequential ewm (taken at T) joined to the windowed-reduction columns, then
        ``assemble`` — the SAME state frame + assemble the fast StatefulEngine path produces (which folds the
        EMAs instead). Held byte-equal to ``compute().last`` by the generic parity test."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        latest = frame["minute"].max()
        frame = frame.with_columns(
            [
                pl.col("close").ewm_mean(span=12).over("symbol").alias("_ema12"),
                pl.col("close").ewm_mean(span=26).over("symbol").alias("_ema26"),
            ]
        )
        frame = frame.with_columns((pl.col("_ema12") - pl.col("_ema26")).ewm_mean(span=9).over("symbol").alias("_macd_signal"))
        state = frame.filter(pl.col("minute") == latest).sort("symbol")
        state = state.join(self.reduction_columns(ctx), on="symbol", how="left")
        feats = self.assemble()
        return state.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]).select(
            ["symbol", "minute", *self.feature_names]
        )

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """BACKFILL form (source of truth): EMAs + RSI/Bollinger/SMA via the rolling forms over every minute,
        then ``assemble`` — bit-compatible with the original hand-written rolling group."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        diff = pl.col("close") - pl.col("_prev")
        gain = pl.when(diff > 0).then(diff).otherwise(0.0)
        loss = pl.when(diff < 0).then(-diff).otherwise(0.0)
        avg_gain = gain.rolling_mean_by("minute", window_size="14m").over("symbol")
        avg_loss = loss.rolling_mean_by("minute", window_size="14m").over("symbol")
        std20 = pl.col("close").rolling_std_by("minute", window_size="20m").over("symbol")
        frame = frame.with_columns(
            [
                (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)).cast(pl.Float64).alias("_rsi_14m"),
                pl.col("close").ewm_mean(span=12).over("symbol").alias("_ema12"),
                pl.col("close").ewm_mean(span=26).over("symbol").alias("_ema26"),
                std20.cast(pl.Float64).alias("_std20"),
            ]
        )
        frame = frame.with_columns((pl.col("_ema12") - pl.col("_ema26")).ewm_mean(span=9).over("symbol").alias("_macd_signal"))
        sma_exprs = [
            pl.col("close").rolling_mean_by("minute", window_size=f"{w}m").over("symbol").alias(f"_sma_{w}")
            for w in SMA_WINDOWS
        ]
        frame = frame.with_columns(sma_exprs)
        feats = self.assemble()
        return frame.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]).select(
            ["symbol", "minute", *self.feature_names]
        )
