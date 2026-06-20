"""Inter-minute trade-count over-dispersion — the Fano factor of the per-minute trade COUNT.

A trade-arrival burstiness primitive (family: TRADE_FLOW, Layer B). The Fano factor (variance-to-mean
ratio) of a counting process measures over-dispersion: for a Poisson (memoryless) arrival stream it is 1;
> 1 means the per-minute trade COUNTS cluster (busy minutes beget busy minutes — self-excitation / a
burst regime), < 1 means they are more regular than Poisson. ``trade_freq_z`` already exposes the z-score
of THIS minute's count vs its trailing baseline (a point anomaly); this is the second-moment SHAPE of the
whole trailing count distribution — how dispersed the activity has been, not whether the latest minute is
a spike — so it is a non-redundant activity-clustering channel.

WHY (feature-invention batch 4, experiments/2026-06-19-feature-invention): in the batch-4 forward-IC
screen ``f_count_fano`` carries fwd-VOLUME IC +0.52 (z 170 vs the within-timestamp shuffle floor) and
fwd-realized-vol IC +0.27 (z 99), stable across the spread days, and is orthogonal to the already-shipped
volume/intensity features (a clustering shape, not a level). It is a forward VOLUME/burst predictor — no
directional alpha (|ret IC| <= 0.02, consistent with the portfolio's direction null).

``count_fano_{w}m`` = ``var(per-minute n_trades) / mean(per-minute n_trades)`` over the trailing ``w``
minutes = ``rolling_std(n_trades, w)**2 / rolling_mean(n_trades, w)`` (the variance is the ddof=1 var the
backfill rolling form and the live kernel both produce). A clean ``ReductionGroup`` (a ratio of a windowed
std**2 to a windowed mean of the SAME per-minute count) — parity-true by construction (compute_latest ==
compute; guarded by tests/test_fp_latest.py). RT-GREEN (two windowed reductions → O(1) incremental, the
~2.5ms floor tier). Null on warmup (need >= 2 minutes for a variance) and when the trailing mean count is
0 (no trades in the window → the ratio is undefined, NOT 0).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, mean_, std_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (60,)
# The trailing mean count is a mean of non-negative per-minute counts (a sum of non-negative terms / n,
# NOT a cancellation difference), so its sign is robust — a plain ``mean > 0`` guard is sufficient and
# correct. A no-trade window -> mean 0 -> NULL on both paths. (Contrast trade_freq_z, whose DENOMINATOR is
# a std that suffers the sign-at-zero cancellation trap and needs a relative floor; here the std is only in
# the NUMERATOR, where a tiny cancellation residual just yields a tiny finite Fano, not a parity flip.)


@register
class CountFanoGroup(ReductionGroup):
    name = "count_fano"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "n_trades")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"count_fano_{w}m",
                description=(
                    f"Fano factor (variance / mean) of the per-minute trade count over the trailing {w} "
                    f"minutes — over-dispersion of trade arrivals. 1 = Poisson, > 1 = clustered/bursty "
                    f"activity (a forward-volume/burst precursor; batch-4 screen fwd-volume IC +0.52, "
                    f"z 170), < 1 = more regular than Poisson. Null on warmup or a no-trade window."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="B",
            )
            for w in WINDOWS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # Unique reduction key ``cf_nt`` (namespaced to this group) so the unified-emit canonical column
        # ``__{stat}_cf_nt_{w}`` cannot collide with any other group's reduction key (the #161 DuplicateError
        # class — range_expansion + realized_range both registered ``rng``).
        return {"cf_nt": (pl.col("n_trades").cast(pl.Float64), ("mean", "std"), WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            # Fano = var/mean = std**2 / mean. Guard the mean > 0 (Guard 2: a mean of non-negative counts,
            # sign-robust) so a no-trade window is NULL on both paths; is_finite() backstop converts any
            # stray non-finite to the agreed NULL identically.
            value = (
                pl.when(mean_("cf_nt", w) > 0.0)
                .then(std_("cf_nt", w).pow(2) / mean_("cf_nt", w))
                .otherwise(pl.lit(None, dtype=pl.Float64))
            )
            feats[f"count_fano_{w}m"] = (
                pl.when(value.is_finite()).then(value).otherwise(pl.lit(None, dtype=pl.Float64))
            )
        return feats
