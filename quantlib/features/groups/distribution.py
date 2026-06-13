"""Return-distribution shape features from per-minute close (family: VOLATILITY, Layer A).

Beyond the mean and variance the other groups capture, the SHAPE of the recent one-minute-return
distribution: skewness (crash vs melt-up asymmetry), excess kurtosis (fat tails / jumpiness), and
the split of variance into downside vs upside semi-deviation. Computed from rolling power sums
(raw moments -> central moments), so it is one vectorized pass and identical live vs backfill. A
null warmup return contributes 0 to every power sum and is excluded from the count, so it never
biases a moment.
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

WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
DIST_TOL = 1e-4


@register
class DistributionGroup(FeatureGroup):
    name = "distribution"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"ret_skew_{w}m", description=f"Skewness of one-minute returns over the trailing {w} minutes (negative = downside-heavy, positive = upside-heavy).",
                            dtype="Float64", valid_range=(-50.0, 50.0), nan_policy="warmup", layer="A", tolerance=DIST_TOL)
            )
            specs.append(
                FeatureSpec(name=f"ret_kurt_{w}m", description=f"Excess kurtosis of one-minute returns over the trailing {w} minutes (fat tails / jumpiness; 0 is Gaussian).",
                            dtype="Float64", valid_range=(-3.0, 1000.0), nan_policy="warmup", layer="A", tolerance=DIST_TOL)
            )
            specs.append(
                FeatureSpec(name=f"downside_vol_{w}m", description=f"Downside semi-deviation of one-minute returns over {w} minutes: root-mean-square of the negative returns only.",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="warmup", layer="A", tolerance=DIST_TOL)
            )
            specs.append(
                FeatureSpec(name=f"upside_vol_{w}m", description=f"Upside semi-deviation of one-minute returns over {w} minutes: root-mean-square of the positive returns only.",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="warmup", layer="A", tolerance=DIST_TOL)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        frame = lagged(frame, "close", 1, "_prev").sort(["symbol", "minute"])
        ret = pl.col("close") / pl.col("_prev") - 1.0
        present = ret.is_not_null()
        r = pl.when(present).then(ret).otherwise(0.0)
        frame = frame.with_columns(
            [
                present.cast(pl.Float64).alias("_p"),
                r.alias("_r1"),
                (r * r).alias("_r2"),
                (r * r * r).alias("_r3"),
                (r * r * r * r).alias("_r4"),
                pl.when(ret < 0.0).then(ret * ret).otherwise(0.0).alias("_dn2"),
                pl.when(ret > 0.0).then(ret * ret).otherwise(0.0).alias("_up2"),
            ]
        )
        exprs = []
        for w in WINDOWS:
            size = f"{w}m"
            n = pl.col("_p").rolling_sum_by("minute", window_size=size).over("symbol")
            s1 = pl.col("_r1").rolling_sum_by("minute", window_size=size).over("symbol")
            s2 = pl.col("_r2").rolling_sum_by("minute", window_size=size).over("symbol")
            s3 = pl.col("_r3").rolling_sum_by("minute", window_size=size).over("symbol")
            s4 = pl.col("_r4").rolling_sum_by("minute", window_size=size).over("symbol")
            dn2 = pl.col("_dn2").rolling_sum_by("minute", window_size=size).over("symbol")
            up2 = pl.col("_up2").rolling_sum_by("minute", window_size=size).over("symbol")
            mean = s1 / n
            m2 = s2 / n - mean * mean
            m3 = s3 / n - 3.0 * mean * (s2 / n) + 2.0 * mean * mean * mean
            m4 = s4 / n - 4.0 * mean * (s3 / n) + 6.0 * mean * mean * (s2 / n) - 3.0 * mean * mean * mean * mean
            defined = (n >= 3.0) & (m2 > 1e-16)
            skew = pl.when(defined).then(m3 / m2.pow(1.5)).otherwise(None).cast(pl.Float64)
            kurt = pl.when(defined).then(m4 / (m2 * m2) - 3.0).otherwise(None).cast(pl.Float64)
            dvol = pl.when(n >= 2.0).then((dn2 / n).sqrt()).otherwise(None).cast(pl.Float64)
            uvol = pl.when(n >= 2.0).then((up2 / n).sqrt()).otherwise(None).cast(pl.Float64)
            exprs.append(skew.alias(f"ret_skew_{w}m"))
            exprs.append(kurt.alias(f"ret_kurt_{w}m"))
            exprs.append(dvol.alias(f"downside_vol_{w}m"))
            exprs.append(uvol.alias(f"upside_vol_{w}m"))
        names = [f"{stat}_{w}m" for w in WINDOWS for stat in ("ret_skew", "ret_kurt", "downside_vol", "upside_vol")]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])
