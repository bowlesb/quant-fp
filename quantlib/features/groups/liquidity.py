"""Liquidity / trading-cost features from per-minute bars + signed flow (family: TRADE_FLOW, Layer B).

Three classic illiquidity estimators over each window:
- **Amihud illiquidity** — mean of |return| / dollar-volume: how much price moves per dollar traded.
- **Roll implied spread** — 2*sqrt(-cov(dp, dp_-1)) / price: the effective spread implied by negative
  autocovariance of consecutive price changes (0 when the autocovariance is non-negative).
- **Kyle's lambda** — slope of price change on signed order flow (via the OLS kernel): price impact
  per share of net buying/selling.

DECOMPOSITION (port onto the fast path): every estimator is an ADDITIVE-WINDOW reduction over short-lag
per-minute columns, so the group is a ``ReductionGroup`` riding the proven windowed-sum kernel:
  * Amihud = a ``mean`` reduction of ``|one-minute return| / dollar-volume``.
  * Roll's autocovariance of consecutive price changes is built from four ``sum`` reductions of the paired
    columns (``_pair`` = both-present count, ``_dpz``/``_dplz`` = the price change and its prior, ``_dpprod``
    = their product), then ``cov = Σprod/n − (Σdp/n)(Σdpl/n)`` — the IDENTICAL algebra as the rolling form.
  * Kyle = the windowed-OLS ``slope`` of the price change (y) on signed volume (x).
All inputs are lag-1 of ``close`` (the prior bar's close, and the prior price change), so they slice-derive
on the incremental path and the running sums equal the rolling backfill cell-for-cell (parity-gated by
tests/test_fp_rest_kinds.py). Amihud/Roll are bar-only; Kyle uses tick-rule ``signed_volume`` (Layer B).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import (
    ReductionGroup,
    mean_,
    pt_,
    slope_,
    sum_,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)


def _prev_close() -> pl.Expr:
    return pl.col("close").shift(1).over("symbol")


def _dp() -> pl.Expr:
    """One-minute price change ``close − close_{-1}`` (null on the first bar)."""
    return pl.col("close") - _prev_close()


def _dp_lag() -> pl.Expr:
    """The prior minute's price change ``close_{-1} − close_{-2}`` (shift of ``_dp``)."""
    return pl.col("close").shift(1).over("symbol") - pl.col("close").shift(2).over("symbol")


@register
class LiquidityGroup(ReductionGroup):
    name = "liquidity"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume", "signed_volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"amihud_illiq_{w}m", description=f"Amihud illiquidity over {w} minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded).",
                            dtype="Float64", valid_range=(0.0, None), nan_policy="warmup", layer="B")
            )
            specs.append(
                FeatureSpec(name=f"roll_spread_{w}m", description=f"Roll implied effective spread over {w} minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative.",
                            dtype="Float64", valid_range=(0.0, 1.0), nan_policy="warmup", layer="B")
            )
            specs.append(
                FeatureSpec(name=f"kyle_lambda_{w}m", description=f"Kyle's lambda over {w} minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid.",
                            dtype="Float64", nan_policy="warmup", layer="B", tolerance=1e-4)
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        dollar = pl.col("close") * pl.col("volume")
        # Amihud = |return| / dollar-volume is UNDEFINED on a no-trade minute (dollar == 0): the ratio
        # overflows to +Inf, which is never a valid illiquidity value and POISONS the trailing-window mean
        # (one zero-volume minute makes amihud +Inf for the whole window). It also breaks live-vs-backfill
        # parity: the incremental running-sum does Inf − Inf = NaN when that minute ages out, so the stream
        # path emits NaN where backfill recovers a finite value. Emit NULL (mathematically-undefined) on a
        # non-positive dollar-volume minute so it is EXCLUDED from the rolling mean on BOTH paths.
        amihud = pl.when(dollar > 0.0).then((pl.col("close") / _prev_close() - 1.0).abs() / dollar).otherwise(
            pl.lit(None, dtype=pl.Float64)
        )
        both = _dp().is_not_null() & _dp_lag().is_not_null()
        dp_z = pl.when(both).then(_dp()).otherwise(0.0)
        dpl_z = pl.when(both).then(_dp_lag()).otherwise(0.0)
        return {
            "amihud": (amihud, ("mean",), WINDOWS),
            "pair": (both.cast(pl.Float64), ("sum",), WINDOWS),
            "dpz": (dp_z, ("sum",), WINDOWS),
            "dplz": (dpl_z, ("sum",), WINDOWS),
            "dpprod": (dp_z * dpl_z, ("sum",), WINDOWS),
        }

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {"kyle": (pl.col("signed_volume"), _dp(), ("slope",), WINDOWS)}

    def points(self) -> dict[str, pl.Expr]:
        return {"close": pl.col("close")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            n, sdp, sdpl, sprod = sum_("pair", w), sum_("dpz", w), sum_("dplz", w), sum_("dpprod", w)
            cov = pl.when(n >= 2.0).then(sprod / n - (sdp / n) * (sdpl / n)).otherwise(None)
            roll = pl.when(cov < 0.0).then(2.0 * (-cov).sqrt() / pt_("close")).otherwise(0.0)
            feats[f"amihud_illiq_{w}m"] = mean_("amihud", w)
            feats[f"roll_spread_{w}m"] = roll
            feats[f"kyle_lambda_{w}m"] = slope_("kyle", w)
        return feats
