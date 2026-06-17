"""Volatility TERM-STRUCTURE — short-horizon vs long-horizon realized vol (family: VOLATILITY).

The platform exposes many vol LEVELS (realized_vol_{w}, parkinson, garman_klass, rogers_satchell,
downside/upside_vol) but NO vol TERM-STRUCTURE: the RATIO of short-horizon to long-horizon realized
vol, i.e. whether vol is EXPANDING (short > long) or CONTRACTING. The R6 study
(experiments/2026-06-17-r6-vol-termstructure, 1,838 syms / 378d) shows this ratio is WELL-SPREAD
(p10/p90 ≈ 0.5/1.3, median ~0.83, 30% expanding) and STRONGLY PERSISTENT (lag-5 autocorr +0.55 in both
liquid and speculative tiers) — a genuine slow-moving vol regime, not minute-noise, and it carries
forward-|return| information with a tier-dependent sign. A tree model splits on thresholds, not on a
ratio of two existing level columns, so the explicit term-structure ratio is genuinely ADDITIVE even
though both realized-vol levels exist. Non-redundant by construction.

Per (symbol, minute):
  - ``vol_term_10_60`` = realized_vol_10m / realized_vol_60m
  - ``vol_term_5_30``  = realized_vol_5m  / realized_vol_30m
where realized_vol is the SAME std-of-1m-returns the ``volatility`` group computes (shared reduction
algebra), so the ratio is consistent with the platform's vol definition. >1 = vol expanding, <1 =
contracting. NULL when the long-horizon denominator is numerically ~0 (a flat window) — guarded by an
absolute floor on long_vol so the stream and backfill paths AGREE on degenerate windows (the
DataIntegrity-4 lesson applied from the start), instead of one side emitting +/-inf.

STATIC windowed feature — a deterministic function of the close buffer, NO FeatureState needed (the
realized_vol reduction pattern). The reduce runs over the full buffer; output keys filter to the latest
minute -> compute_latest == compute().last by construction (parity-guarded).
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, std_
from quantlib.features.registry import register

# (short, long) horizon pairs; the reduction windows are the union.
TERM_PAIRS: tuple[tuple[int, int], ...] = ((10, 60), (5, 30))
_VOL_WINDOWS: tuple[int, ...] = tuple(sorted({w for pair in TERM_PAIRS for w in pair}))
# Degeneracy guard: a long-horizon realized vol (a 1m-return std, typically ~1e-4..1e-2) below this
# absolute floor is a numerically-flat window where short/long overflows to +/-inf; emit NULL there so
# the stream and backfill paths AGREE on degenerate flat windows (the DataIntegrity-4 lesson).
_VOL_FLOOR = 1e-9


@register
class VolTermStructureGroup(ReductionGroup):
    name = "vol_term_structure"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"vol_term_{short}_{long}",
                description=(
                    f"Volatility term-structure: realized_vol_{short}m / realized_vol_{long}m (std of 1m "
                    f"returns). >1 = vol EXPANDING (short-horizon vol above long), <1 = contracting. "
                    f"NULL on a degenerate flat long-horizon window."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
                tolerance=0.02,
            )
            for short, long in TERM_PAIRS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {"ret": (ret, ("std",), _VOL_WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for short, long in TERM_PAIRS:
            short_vol = std_("ret", short)
            long_vol = std_("ret", long)
            feats[f"vol_term_{short}_{long}"] = (
                pl.when(long_vol > _VOL_FLOOR)
                .then(short_vol / long_vol)
                .otherwise(pl.lit(None, dtype=pl.Float64))
            )
        return feats
