"""OHLC-efficient volatility estimators from per-minute bars (family: VOLATILITY, Layer A).

Garman-Klass and Rogers-Satchell use the full open/high/low/close of each bar, so they extract far
more volatility information per minute than a close-to-close std. Both are per-bar variance estimators
averaged over the window then square-rooted. Pure OHLC arithmetic -> identical live and backfill.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120)


@register
class OhlcVolGroup(ReductionGroup):
    name = "ohlc_vol"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "open", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"garman_klass_vol_{w}m", description=f"Garman-Klass volatility over {w} minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted.",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"rogers_satchell_vol_{w}m", description=f"Rogers-Satchell volatility over {w} minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted.",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="warmup", layer="A")
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ln_hl = (pl.col("high") / pl.col("low")).log()
        ln_co = (pl.col("close") / pl.col("open")).log()
        ln_hc = (pl.col("high") / pl.col("close")).log()
        ln_ho = (pl.col("high") / pl.col("open")).log()
        ln_lc = (pl.col("low") / pl.col("close")).log()
        ln_lo = (pl.col("low") / pl.col("open")).log()
        gk_var = 0.5 * ln_hl * ln_hl - (2.0 * 0.6931471805599453 - 1.0) * ln_co * ln_co
        rs_var = ln_hc * ln_ho + ln_lc * ln_lo
        return {"gk": (gk_var, ("mean",), WINDOWS), "rs": (rs_var, ("mean",), WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            feats[f"garman_klass_vol_{w}m"] = mean_("gk", w).clip(0.0, None).sqrt()
            feats[f"rogers_satchell_vol_{w}m"] = mean_("rs", w).clip(0.0, None).sqrt()
        return feats
