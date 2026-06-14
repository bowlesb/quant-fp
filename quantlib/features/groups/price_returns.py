"""Price returns over many trailing minute windows (family: PRICE).

Simple and log close-to-close returns, point-in-time, all sessions. A return at T is a POINT lag — the
close as of minute T − w — not a window reduction. So the group rides the LAG / last-k state KIND
(docs/STATE_ABSTRACTION.md): a per-symbol ring of recent closes keyed by minute-epoch, with one
``LagSpec`` per window (``_lag{w}`` = close as of T − w, null when that exact minute is absent —
identical to ``base.lagged``, correct on gappy grids). The live fast path folds one minute into the ring
and reads each lag in O(1); the backfill reaches the SAME lag columns with a TIME-based self-join. Both
evaluate the SAME ``assemble`` (the ratio/log of close over each lag), so live and backfill are
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
from quantlib.features.stateful import LagSpec, StatefulGroup

WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 45, 60, 90, 120, 180)


@register
class PriceReturnGroup(StatefulGroup):
    name = "price_returns"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"ret_{w}m",
                    description=f"Simple close-to-close return over the trailing {w} minute(s), point-in-time as of the minute open.",
                    dtype="Float64",
                    valid_range=(-1.0, 5.0),
                    nan_policy="warmup",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"log_ret_{w}m",
                    description=f"Log close-to-close return ln(close/close_-{w}m) over the trailing {w} minute(s), point-in-time.",
                    dtype="Float64",
                    valid_range=(-5.0, 5.0),
                    nan_policy="warmup",
                )
            )
        return specs

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """At-T columns the state frame carries: close (the ring's lag source + the ratio numerator)."""
        return frame

    def lag_specs(self) -> list[LagSpec]:
        return [LagSpec(alias=f"_lag{w}", source="close", minutes=w) for w in WINDOWS]

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            ratio = pl.col("close") / pl.col(f"_lag{w}")
            feats[f"ret_{w}m"] = ratio - 1.0
            feats[f"log_ret_{w}m"] = ratio.log()
        return feats
