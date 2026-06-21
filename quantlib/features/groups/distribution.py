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
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
DIST_TOL = 1e-4


@register
class DistributionGroup(ReductionGroup):
    name = "distribution"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # NO-GO for FP_INCREMENTAL: the higher-moment stats (skew/kurtosis) are 3rd/4th power-sums whose
    # Σx⁴−... cancellation the real-data A/B soak (docs/INCREMENTAL_READINESS.md, 2026-06-17) finds breaching
    # on real gappy tape (ret_kurt_10m, 0.4% of minutes, worst ~10404x) — the higher-moment analogue of the
    # parked corr-denom class (the synthetic degenerate stream triggers this one but not the gappier 5).
    # Stays on the batch path under FP_INCREMENTAL until a cancellation-free higher-moment reduction lands.
    incremental_safe = False

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

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        present = ret.is_not_null()
        r = pl.when(present).then(ret).otherwise(0.0)  # warmup-null return -> 0 (excluded from the count)
        return {
            "p": (present.cast(pl.Float64), ("sum",), WINDOWS),  # count of present returns
            "r1": (r, ("sum",), WINDOWS),
            "r2": (r * r, ("sum",), WINDOWS),
            "r3": (r * r * r, ("sum",), WINDOWS),
            "r4": (r * r * r * r, ("sum",), WINDOWS),
            "dn2": (pl.when(ret < 0.0).then(ret * ret).otherwise(0.0), ("sum",), WINDOWS),
            "up2": (pl.when(ret > 0.0).then(ret * ret).otherwise(0.0), ("sum",), WINDOWS),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            n = sum_("p", w)
            s1, s2, s3, s4 = sum_("r1", w), sum_("r2", w), sum_("r3", w), sum_("r4", w)
            mean = s1 / n
            m2 = s2 / n - mean * mean
            m3 = s3 / n - 3.0 * mean * (s2 / n) + 2.0 * mean * mean * mean
            m4 = s4 / n - 4.0 * mean * (s3 / n) + 6.0 * mean * mean * (s2 / n) - 3.0 * mean * mean * mean * mean
            defined = (n >= 3.0) & (m2 > 1e-16)
            feats[f"ret_skew_{w}m"] = pl.when(defined).then(m3 / m2.pow(1.5)).otherwise(None)
            feats[f"ret_kurt_{w}m"] = pl.when(defined).then(m4 / (m2 * m2) - 3.0).otherwise(None)
            # sum(dn2)/sum(up2) are sums of SQUARED returns -> mathematically NON-NEGATIVE, but the live
            # incremental running sum can drift to a tiny negative residue when a window holds only zero
            # contributions (a sparse symbol whose in-window returns are all one-signed -> the other side is
            # all zeros), whose sqrt is NaN while backfill's rolling sum stays exactly 0.0. Clip >=0 before
            # the sqrt (matching ohlc_vol / volatility) so live == backfill on sparse symbols.
            feats[f"downside_vol_{w}m"] = pl.when(n >= 2.0).then((sum_("dn2", w) / n).clip(lower_bound=0.0).sqrt()).otherwise(None)
            feats[f"upside_vol_{w}m"] = pl.when(n >= 2.0).then((sum_("up2", w) / n).clip(lower_bound=0.0).sqrt()).otherwise(None)
        return feats
