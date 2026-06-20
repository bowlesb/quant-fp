"""Volume features from per-minute bars over windows (family: VOLUME, Layer A).

Migrated to the declarative reduction engine: it declares ``reduced``/``points``/``assemble`` ONCE and the
engine generates both the rolling backfill form and the at-T live form (parity by construction). See
quantlib/features/declarative.py.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_, pt_, std_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)

# A volume std below this fraction of the window mean is a degenerate constant-volume window where the
# z-score is undefined (and a bare `std > 0` / exact `std == 0` guard diverges stream-vs-backfill on
# float rounding); we emit NULL there so both paths agree (parity). The floor must be ABOVE the std float
# noise the two paths disagree by: on a genuinely-constant window the live power-sum std is EXACTLY 0.0
# while backfill `rolling_std_by` (Welford, sliding add/remove) leaves a residue ~ a few * 1e-9 of the mean
# (measured 2.8e-6 on volume==1000, i.e. ~2.8e-9 relative). A 1e-9 floor sits AT that noise level, so the
# residue lands ABOVE it (backfill passes -> z=0) while the live 0.0 lands below (-> NULL): a null/non-null
# parity break. 1e-6 swallows the Welford residue with ~1000x margin and stays far below any real volume
# z-score (genuine intraday volume std/mean is O(0.1-1)), so well-conditioned windows are untouched.
_VOL_STD_REL_EPS = 1e-6


@register
class VolumeGroup(ReductionGroup):
    name = "volume"
    version = "1.1.0"
    owner = "modeller"
    type = FeatureType.VOLUME
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),
    )
    windows = WINDOWS
    # volume_zscore divides by std(ddof=1). REMAINS GATED (the only one of the 4 not flippable by the n==2 OLS
    # guard): its blocker is a VARIANCE-family cancellation, not the OLS corner. Backfill (truth) computes std via
    # polars ``rolling_std_by`` (Welford, stable); the live/incremental path computes it from power sums as
    # ``sqrt((Σv² − (Σv)²/n)/(n−1))`` (catastrophic cancellation on RAW share volume ~1e6). On a near-constant
    # huge-volume window the two land on OPPOSITE sides of the relative null-floor (``_VOL_STD_REL_EPS``) — a
    # null/non-null parity break — AND at the n=2/3 z-score they disagree by ~7e-4 (verified, fresh-seed: the
    # break is present even with ZERO incremental running-drift, so it is a batch-vs-canonical std FORMULA gap,
    # not engine drift). The parity-true fix is the centered power-sum std (store Σ(v−c)/Σ(v−c)² for a
    # reproducible per-symbol c) in the SHARED batch kernel so backfill and live compute std identically — that
    # touches the batch path (Lead-owned, invasive). Flip to True only after that lands and parity-gates clean.
    incremental_safe = False

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="dollar_volume_1m",
                description="Dollar volume traded in the last minute (close price * share volume).",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="none",
                layer="A",
            )
        ]
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"volume_zscore_{w}m",
                    description=f"Z-score of the last minute's share volume vs the trailing {w}-minute mean and std.",
                    dtype="Float64",
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"volume_ratio_{w}m",
                    description=f"Ratio of the last minute's share volume to its trailing {w}-minute mean.",
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {"volume": (pl.col("volume"), ("mean", "std"), WINDOWS)}

    def points(self) -> dict[str, pl.Expr]:
        return {"volT": pl.col("volume"), "dv": pl.col("close") * pl.col("volume")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {"dollar_volume_1m": pt_("dv")}
        for w in WINDOWS:
            std = std_("volume", w)
            mean_w = mean_("volume", w)
            zscore = (pt_("volT") - mean_w) / std
            # std is null during warmup (<2 samples) -> keep null. A bare `std > 0` guard is too LOOSE:
            # a near-constant-volume window gives std ~1e-9 (passes) and either blows the z-score up or,
            # via the EXACT `std == 0` branch, diverges when one path's std rounds to 1e-15 and the
            # other to exactly 0. Guard on a RELATIVE threshold (std a non-trivial fraction of the mean)
            # and emit NULL on the degenerate constant-volume window so stream and backfill agree.
            feats[f"volume_zscore_{w}m"] = (
                pl.when(std > _VOL_STD_REL_EPS * mean_w.abs())
                .then(zscore)
                .otherwise(pl.lit(None, dtype=pl.Float64))
            )
            feats[f"volume_ratio_{w}m"] = pt_("volT") / mean_w
        return feats
