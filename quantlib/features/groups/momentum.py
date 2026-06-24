"""Momentum / trend-consistency features from per-minute close (family: MOMENTUM, Layer A)."""

from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, mean_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)

# These features average one-minute RETURNS over a trailing window. A minute's settled (backfill)
# close is the consolidated tape (late/out-of-sequence prints applied overnight); the live websocket
# bar is provisional and cannot see those revisions. A single revised close perturbs the return at TWO
# minutes (it is the numerator of one return and the denominator of the next), so any window that spans
# a revised minute disagrees live-vs-settled — the irreducible Layer-A "~99.5%" revision class
# (docs/PARITY_PLAYBOOK.md, docs/DATA_QUALITY_LEDGER.md). Exact-cell parity therefore false-fails these
# (and worse the longer the window, since a wider window is likelier to contain a revised minute), so
# they are graded DISTRIBUTIONALLY: the live and settled VALUE DISTRIBUTIONS must agree (quantile shape
# + paired-fraction), which they do. The 1% tolerance is the measured worst-quantile divergence on the
# real 06-23 tape (up_ratio_45m, 0.9%) — entirely revision noise, not a compute difference. This is
# NOT a path bug: neither path is wrong; live simply can't see future corrections (root-cause 06-24).
REVISION_DIST_TOL = 0.01


@register
class MomentumGroup(ReductionGroup):
    name = "momentum"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"up_ratio_{w}m",
                    description=f"Fraction of the trailing {w} minutes with a positive one-minute return (0-1).",
                    dtype="Float64",
                    valid_range=(-0.01, 1.01),
                    nan_policy="warmup",
                    layer="A",
                    parity_method="distributional",
                    tolerance=REVISION_DIST_TOL,
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"mean_abs_ret_{w}m",
                    description=f"Mean absolute one-minute return over the trailing {w} minutes (choppiness).",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                    parity_method="distributional",
                    tolerance=REVISION_DIST_TOL,
                )
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {
            "up": ((ret > 0.0).cast(pl.Float64), ("mean",), WINDOWS),  # fraction of up minutes
            "absret": (ret.abs(), ("mean",), WINDOWS),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            feats[f"up_ratio_{w}m"] = mean_("up", w)
            feats[f"mean_abs_ret_{w}m"] = mean_("absret", w)
        return feats
