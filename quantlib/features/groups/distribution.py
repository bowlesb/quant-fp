"""Return-distribution shape features from per-minute close (family: VOLATILITY, Layer A).

Beyond the mean and variance the other groups capture, the SHAPE of the recent one-minute-return
distribution: skewness (crash vs melt-up asymmetry), excess kurtosis (fat tails / jumpiness), and
the split of variance into downside vs upside semi-deviation. Computed from rolling power sums
(raw moments -> central moments), so it is one vectorized pass and identical live vs backfill. A
null warmup return contributes 0 to every power sum and is excluded from the count, so it never
biases a moment.

The skew/kurtosis power sums center the return on a per-symbol constant anchor (``Σ(r−a)^k``) and a
defined-guard nulls degenerate float-noise-variance windows, so the higher central moments are
conditioned and live == backfill cell-for-cell; the moments are translation-invariant, so the
centering leaves the feature VALUE unchanged. See ``incremental_safe`` below.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, sum_
from quantlib.features.reduction_anchor import anchor_column
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
DIST_TOL = 1e-4
_ANCHOR_RETURN = anchor_column("return")  # per-symbol return-centering anchor the power sums center on

# Minimum return-variance (m2) for skew/kurtosis to be DEFINED. A one-minute-return variance below this is a
# degenerate near-constant-return window: the variance is float noise (a real intraday one-minute-return std is
# O(1e-3) -> m2 O(1e-6) and up; 1e-12 sits ~6 orders of magnitude below that and ~3 above the float-noise m2
# ~1e-15 the degenerate cells carry). On such a cell the 3rd/4th central moment is a difference of near-equal
# tiny terms, so the incremental running-sum and the batch fresh-sum round the meaningless kurtosis differently
# (the old 1e-16 guard let it through -> the incremental==batch breach that parked the group). Nulling it at a
# floor that sits in the GAP between degenerate and real variance makes both paths agree (both null) while
# leaving every genuine window (m2 >> floor) defined and unchanged. Mirrors volume's _VOL_STD_REL_EPS and the
# OLS denom floors — a conditioning guard placed above the float-noise band, not a value change on real data.
_MOMENT_MIN_VAR = 1e-12


@register
class DistributionGroup(ReductionGroup):
    name = "distribution"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", _ANCHOR_RETURN)),)
    # Previously NO-GO for the incremental fast path: ret_skew/ret_kurt breached incremental==batch on a near-
    # CONSTANT-but-nonzero-return window (a symbol ticking a fixed fraction/min) — the 3rd/4th central moment is
    # a difference of near-equal ~r^k terms, so the running-sum and batch fresh-sum rounded it differently and
    # the defined-guard flipped (~0.4% of real minutes, worst ~10404x at ret_kurt_10m). CLOSED by two
    # value-faithful conditioning fixes, NOT a tolerance loosening: (1) center the return on a per-symbol
    # constant anchor before the power sums (Σ(r−a)^k, reduction_anchor.attach_return_anchor — translation-
    # invariant so the value is unchanged, but (r−a) stays small so the cancellation is conditioned); (2) raise
    # the defined-guard from m2 > 1e-16 to m2 > _MOMENT_MIN_VAR (1e-12), nulling the degenerate float-noise-
    # variance cells where the moment is meaningless and the two paths cannot agree — both paths now null them.
    # The engine and batch agree cell-for-cell on the degenerate-stream gate (tests/test_fp_incremental_features).
    incremental_safe = True

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
        # Center the return on the per-symbol constant anchor BEFORE the power sums (Σ(r−a)^k): the central
        # moments are translation-invariant so this is value-identical, but (r−a) stays small on a near-constant
        # window so the 3rd/4th-moment cancellation is conditioned. The anchor is a plain per-symbol column read
        # identically by both paths (parity-critical). Warmup-null return -> 0 (excluded from the count).
        rc = pl.when(present).then(ret - pl.col(_ANCHOR_RETURN)).otherwise(0.0)
        return {
            "p": (present.cast(pl.Float64), ("sum",), WINDOWS),  # count of present returns
            "c1": (rc, ("sum",), WINDOWS),  # Σ(r−a)
            "c2": (rc * rc, ("sum",), WINDOWS),
            "c3": (rc * rc * rc, ("sum",), WINDOWS),
            "c4": (rc * rc * rc * rc, ("sum",), WINDOWS),
            "dn2": (pl.when(ret < 0.0).then(ret * ret).otherwise(0.0), ("sum",), WINDOWS),
            "up2": (pl.when(ret > 0.0).then(ret * ret).otherwise(0.0), ("sum",), WINDOWS),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            n = sum_("p", w)
            # Central moments of the CENTERED return (r−a) — equal to the central moments of r itself (a shift
            # leaves every central moment unchanged). ``mean`` here is mean(r−a) = mean(r) − a.
            s1, s2, s3, s4 = sum_("c1", w), sum_("c2", w), sum_("c3", w), sum_("c4", w)
            mean = s1 / n
            m2 = s2 / n - mean * mean
            m3 = s3 / n - 3.0 * mean * (s2 / n) + 2.0 * mean * mean * mean
            m4 = s4 / n - 4.0 * mean * (s3 / n) + 6.0 * mean * mean * (s2 / n) - 3.0 * mean * mean * mean * mean
            defined = (n >= 3.0) & (m2 > _MOMENT_MIN_VAR)
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
