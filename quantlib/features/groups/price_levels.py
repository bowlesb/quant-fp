"""Price-level features: where close sits within its recent range (family: PRICE, Layer A).

Time-anchored rolling high/low so the window is wall-clock, correct on gappy grids. Each feature is a
distance from a trailing MAX (high) or MIN (low), so the group rides the ROLLING-EXTREMA state KIND
(docs/STATE_ABSTRACTION.md): a per-(symbol, window) monotonic deque that yields the trailing max-high /
min-low in O(1) amortized. The live fast path folds one minute into the deques; the backfill reaches the
SAME high/low columns with ``rolling_max_by`` / ``rolling_min_by``. Both evaluate the SAME ``assemble``
(position-in-range and distance-from-high/low from the current close), so live and backfill are
cell-for-cell identical — parity by construction, guarded by tests/test_fp_rest_kinds.py.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register
from quantlib.features.stateful import ExtremaSpec, StatefulGroup

WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120, 240)


@register
class PriceLevelGroup(StatefulGroup):
    name = "price_levels"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "high", "low")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"position_in_range_{w}m",
                    description=f"Where close sits in its trailing {w}-minute high-low range: (close - min_low) / (max_high - min_low).",
                    dtype="Float64",
                    valid_range=(-0.01, 1.01),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"dist_from_high_{w}m",
                    description=f"Close relative to the trailing {w}-minute high (close / max_high - 1); <= 0.",
                    dtype="Float64",
                    valid_range=(-1.0, 0.01),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"dist_from_low_{w}m",
                    description=f"Close relative to the trailing {w}-minute low (close / min_low - 1); >= 0.",
                    dtype="Float64",
                    valid_range=(-0.01, 5.0),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """At-T columns the state frame carries: close (the ratio numerator); high/low are the extrema sources."""
        return frame

    def extrema_specs(self) -> list[ExtremaSpec]:
        specs: list[ExtremaSpec] = []
        for w in WINDOWS:
            specs.append(ExtremaSpec(alias=f"_hi_{w}", source="high", window=w, op="max"))
            specs.append(ExtremaSpec(alias=f"_lo_{w}", source="low", window=w, op="min"))
        return specs

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            high_w, low_w, close_t = pl.col(f"_hi_{w}"), pl.col(f"_lo_{w}"), pl.col("close")
            feats[f"position_in_range_{w}m"] = (close_t - low_w) / (high_w - low_w)
            feats[f"dist_from_high_{w}m"] = close_t / high_w - 1.0
            feats[f"dist_from_low_{w}m"] = close_t / low_w - 1.0
        return feats
