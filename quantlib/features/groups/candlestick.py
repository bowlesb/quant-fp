"""Candlestick-shape features from per-minute OHLC (family: CANDLESTICK, Layer A).

Pure per-bar arithmetic on open/high/low/close, plus two-candle patterns that read the prior bar via
the time-based ``lagged`` helper. Every value is a deterministic function of the same OHLC the live
stream and the historical backfill both deliver, so parity holds by construction. Zero-range bars
(high == low) are mapped to 0 ratios rather than NaN so the declared ranges stay clean.
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

RATIO_RANGE: tuple[float, float] = (-0.01, 1.01)


@register
class CandlestickGroup(FeatureGroup):
    name = "candlestick"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CANDLESTICK
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "open", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        single = [
            ("body_ratio", "Real-body size as a fraction of the bar's high-low range: |close-open| / (high-low); 0 when the range is zero."),
            ("upper_shadow_ratio", "Upper wick as a fraction of the high-low range: (high - max(open,close)) / (high-low)."),
            ("lower_shadow_ratio", "Lower wick as a fraction of the high-low range: (min(open,close) - low) / (high-low)."),
            ("is_bullish", "1.0 when the minute closed above its open (a green/up bar), else 0.0."),
            ("is_doji", "1.0 when the real body is under 10% of the bar range (indecision/doji), else 0.0."),
            ("is_hammer", "1.0 for a hammer: long lower wick (>60% of range), tiny upper wick, small body."),
            ("is_shooting_star", "1.0 for a shooting star: long upper wick (>60% of range), tiny lower wick, small body."),
            ("is_marubozu", "1.0 for a marubozu: real body fills over 90% of the bar range (almost no wicks)."),
        ]
        two = [
            ("pattern_engulfing_bullish", "1.0 when a bullish bar's body fully engulfs the prior bearish bar's body (two-candle reversal)."),
            ("pattern_engulfing_bearish", "1.0 when a bearish bar's body fully engulfs the prior bullish bar's body (two-candle reversal)."),
            ("pattern_harami_bullish", "1.0 when a small bullish bar's body sits inside the prior larger bearish bar's body (harami)."),
            ("pattern_harami_bearish", "1.0 when a small bearish bar's body sits inside the prior larger bullish bar's body (harami)."),
        ]
        specs = [
            FeatureSpec(name=name, description=desc, dtype="Float64", valid_range=RATIO_RANGE, nan_policy="none", layer="A")
            for name, desc in single
        ]
        specs += [
            FeatureSpec(name=name, description=desc, dtype="Float64", valid_range=RATIO_RANGE, nan_policy="warmup", layer="A")
            for name, desc in two
        ]
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "open", "high", "low", "close"])
        frame = lagged(frame, "open", 1, "_prev_open")
        frame = lagged(frame, "close", 1, "_prev_close").sort(["symbol", "minute"])

        rng = pl.col("high") - pl.col("low")
        positive = rng > 0.0
        abs_body = (pl.col("close") - pl.col("open")).abs()
        body_top = pl.max_horizontal("open", "close")
        body_bottom = pl.min_horizontal("open", "close")
        body_ratio = pl.when(positive).then(abs_body / rng).otherwise(0.0)
        upper_ratio = pl.when(positive).then((pl.col("high") - body_top) / rng).otherwise(0.0)
        lower_ratio = pl.when(positive).then((body_bottom - pl.col("low")) / rng).otherwise(0.0)

        prev_bear = pl.col("_prev_close") < pl.col("_prev_open")
        prev_bull = pl.col("_prev_close") > pl.col("_prev_open")
        curr_bull = pl.col("close") > pl.col("open")
        curr_bear = pl.col("close") < pl.col("open")

        frame = frame.with_columns(
            [
                body_ratio.cast(pl.Float64).alias("body_ratio"),
                upper_ratio.cast(pl.Float64).alias("upper_shadow_ratio"),
                lower_ratio.cast(pl.Float64).alias("lower_shadow_ratio"),
                curr_bull.cast(pl.Float64).alias("is_bullish"),
            ]
        )
        frame = frame.with_columns(
            [
                (pl.col("body_ratio") < 0.1).cast(pl.Float64).alias("is_doji"),
                ((pl.col("lower_shadow_ratio") > 0.6) & (pl.col("upper_shadow_ratio") < 0.1) & (pl.col("body_ratio") < 0.4)).cast(pl.Float64).alias("is_hammer"),
                ((pl.col("upper_shadow_ratio") > 0.6) & (pl.col("lower_shadow_ratio") < 0.1) & (pl.col("body_ratio") < 0.4)).cast(pl.Float64).alias("is_shooting_star"),
                (pl.col("body_ratio") > 0.9).cast(pl.Float64).alias("is_marubozu"),
                (prev_bear & curr_bull & (pl.col("close") >= pl.col("_prev_open")) & (pl.col("open") <= pl.col("_prev_close"))).cast(pl.Float64).alias("pattern_engulfing_bullish"),
                (prev_bull & curr_bear & (pl.col("open") >= pl.col("_prev_close")) & (pl.col("close") <= pl.col("_prev_open"))).cast(pl.Float64).alias("pattern_engulfing_bearish"),
                (prev_bear & curr_bull & (body_top <= pl.col("_prev_open")) & (body_bottom >= pl.col("_prev_close"))).cast(pl.Float64).alias("pattern_harami_bullish"),
                (prev_bull & curr_bear & (body_top <= pl.col("_prev_close")) & (body_bottom >= pl.col("_prev_open"))).cast(pl.Float64).alias("pattern_harami_bearish"),
            ]
        )
        names = [spec.name for spec in self.declare()]
        return frame.select(["symbol", "minute", *names])
