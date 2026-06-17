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
        # z = (count_now - rolling_mean) / rolling_std; null when std is null/0 (undefined, not 0).
        return {
            f"trade_freq_z_{w}m": pl.when(std_("nt", w) > 0)
            .then((pt_("nt1") - mean_("nt", w)) / std_("nt", w))
            .otherwise(None)
            for w in WINDOWS
        }
