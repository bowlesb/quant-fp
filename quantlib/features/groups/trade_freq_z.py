"""Trade-frequency z-score — how anomalous is THIS minute's trade count vs the name's recent baseline.

A normalized activity-BURST primitive (family: TRADE_FLOW, Layer B). The `microstructure_burst` group already
exposes intensity (peak trades/sec, inter-arrival CV, active seconds); what it lacks is the trailing
**z-score of the per-minute trade COUNT** — i.e. is this minute's activity a violent surge vs the name's own
recent typical level. This is an attention / information-shock proxy surfaced by the W14 activity-burst study
(the burst detector was "a clean reusable primitive"); even though the standalone 2-day burst drift was
catalyst-driven, the normalized frequency is a real, non-redundant activity-state feature the all-features
model can use in combination.

`trade_freq_z_{w}m` = (n_trades − rolling_mean(n_trades, w)) / rolling_std(n_trades, w) over the trailing w
minutes — point-in-time, parity-true by construction (a `ReductionGroup`: compute_latest == compute, guarded
by tests/test_fp_latest.py). Null on warmup (need ≥2 minutes for a std) and when the trailing std is 0
(mathematically undefined, NOT 0 — a flat-activity window).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, mean_, pt_, std_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
# A constant-count window's std is mathematically 0, but the backfill rolling form computes it as
# sqrt(Σv² − (Σv)²/n) — a catastrophic cancellation that lands on a tiny FINITE std/mean ratio (~1e-8 at
# small integer counts) while the live rust kernel returns exactly 0.0. A bare ``std > 0`` guard then passes
# on backfill (emitting z=0) and fails on live (emitting NULL): the #122 std-sign-at-zero parity break, here
# on a flat trade-COUNT window (e.g. an illiquid name printing the same count each minute). Require std to be
# a non-trivial fraction of the mean count so a flat-activity window is NULL on BOTH paths; 1e-6 dominates the
# ~1e-8 cancellation residual by ~100x and sits far below any real per-minute count dispersion (a single
# off-count gives std/mean >> 0.01), so genuine activity bursts are untouched. Mirrors volume_zscore's
# ``_VOL_STD_REL_EPS`` (volume's ~1e3-1e4 scale needs only 1e-9; small integer counts need a higher floor).
_TFZ_STD_REL_EPS = 1e-6


@register
class TradeFreqZGroup(ReductionGroup):
    name = "trade_freq_z"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "n_trades")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"trade_freq_z_{w}m",
                description=(
                    f"Z-score of this minute's trade count vs the trailing {w}-minute rolling mean/std of "
                    f"the symbol's per-minute trade count — a normalized activity-burst / attention proxy."
                ),
                dtype="Float64",
                nan_policy="warmup",
                layer="B",
            )
            for w in WINDOWS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {"nt": (pl.col("n_trades"), ("mean", "std"), WINDOWS)}

    def points(self) -> dict[str, pl.Expr]:
        return {"nt1": pl.col("n_trades")}

    def assemble(self) -> dict[str, pl.Expr]:
        # z = (count_now - rolling_mean) / rolling_std; null on warmup (<2 samples) and on a flat-count
        # window (std a relative ~0) — undefined, not 0 — so stream and backfill agree (see _TFZ_STD_REL_EPS).
        return {
            f"trade_freq_z_{w}m": pl.when(std_("nt", w) > _TFZ_STD_REL_EPS * mean_("nt", w).abs())
            .then((pt_("nt1") - mean_("nt", w)) / std_("nt", w))
            .otherwise(None)
            for w in WINDOWS
        }
