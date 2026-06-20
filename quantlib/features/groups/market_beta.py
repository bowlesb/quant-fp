"""Market-relationship features: how a ticker co-moves with SPY (family: CROSS_SECTIONAL, Layer A).

Per symbol, regress the one-minute return on SPY's one-minute return over each window (shared
windowed-OLS kernel): the slope is rolling beta, the correlation is how tightly it tracks the market,
and idiosyncratic volatility is the part of its movement the market does not explain. SPY is already
in minute_agg (streamed + backfilled as a market-context symbol), so this is pure-bar and parity-true
by construction — the same minute_agg feeds the same regression live and in backfill.

DECOMPOSITION (port onto the fast path): every feature here is a MARKET-RELATIVE windowed reduction over
paired sums, so the group is a ``ReductionGroup`` riding the proven additive-window kernel:
  * ``market_beta``/``market_corr`` are the slope/corr of the windowed OLS of the ticker's one-minute return
    (y, a per-symbol short-lag column) on SPY's one-minute return (x). SPY's return is the SAME for every
    symbol at a minute, so it is a CROSS-SYMBOL BROADCAST — ``prepare`` joins it onto every row for the
    polars backfill/live forms, and the incremental engine sources it via a ``broadcast`` StatefulRegressor
    (read SPY's row, broadcast to the universe) instead of re-deriving over the buffer.
  * ``idio_vol`` = the ticker's return std (a windowed ``std`` reduction) times ``sqrt(1 - r2)`` (the
    regression's r2). Both ride the same kernel pass.
Backfill is the rolling source of truth; live-batch and the incremental fold both reach the SAME paired
sums, so beta/corr/r2 are cell-for-cell identical (parity-gated by tests/test_fp_market_relative.py).
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
    StatefulRegressor,
    corr_,
    r2_,
    slope_,
    std_,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (10, 15, 30, 45, 60, 90, 120)
MARKET_TICKER = "SPY"
BETA_TOL = 1e-4
# |beta| beyond the declared valid range is a degenerate near-zero-variance OLS fit (thin/after-hours
# windows where SPY barely moves: cov/tiny-denom explodes). Null it rather than ship a non-physical beta.
BETA_MAX = 15.0


@register
class MarketBetaGroup(ReductionGroup):
    name = "market_beta"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # The OLS regressor x is SPY's one-minute return broadcast onto every symbol. On a gappy symbol the few
    # in-window paired bars give a near-constant x, so the corr denominator b·Σx²−(Σx)² is a difference of
    # float-noise that incremental's running sum rounds differently from the batch fresh sum, straddling the
    # corr defined-guard — incremental emits market_corr=±1 / idio_vol=0 where batch NULLs (real 06-18 gappy
    # A/B: MO/SLB). Same conditioning class as the gated correlation groups; routed LIVE to the batch path.
    # (Smooth-synthetic gappy sweep missed it: no SPY symbol -> _mret all-null -> corr-denom unexercised.)
    incremental_safe = False

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"market_beta_{w}m", description=f"Rolling beta to SPY over {w} minutes: slope of this ticker's one-minute return regressed on SPY's.",
                            dtype="Float64", valid_range=(-15.0, 15.0), nan_policy="sparse", layer="A", tolerance=BETA_TOL)
            )
            specs.append(
                FeatureSpec(name=f"market_corr_{w}m", description=f"Rolling correlation of this ticker's one-minute return with SPY's over {w} minutes, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A", tolerance=BETA_TOL)
            )
            specs.append(
                FeatureSpec(name=f"idio_vol_{w}m", description=f"Idiosyncratic volatility over {w} minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain).",
                            dtype="Float64", valid_range=(0.0, 5.0), nan_policy="sparse", layer="A", tolerance=BETA_TOL)
            )
        return specs

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Broadcast SPY's one-minute return onto every symbol's row by a minute-join (the cross-symbol market
        regressor). The polars backfill/live forms read ``_mret`` from this; the incremental engine sources it
        from running state (the ``broadcast`` regressor) and ignores this column."""
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        market = (
            frame.with_columns(ret.alias("_ret"))
            .filter(pl.col("symbol") == MARKET_TICKER)
            .select(["minute", pl.col("_ret").alias("_mret")])
        )
        return frame.join(market, on="minute", how="left")

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {"_ret": (ret, ("std",), WINDOWS)}

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {"mkt": (pl.col("_mret"), ret, ("slope", "corr", "r2"), WINDOWS)}

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        index_ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {
            "mkt": [StatefulRegressor(slot="x", kind="broadcast", increment=index_ret, broadcast_symbol=MARKET_TICKER)]
        }

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            beta = slope_("mkt", w)
            feats[f"market_beta_{w}m"] = pl.when(beta.abs() <= BETA_MAX).then(beta).otherwise(None)
            feats[f"market_corr_{w}m"] = corr_("mkt", w)
            feats[f"idio_vol_{w}m"] = std_("_ret", w) * (1.0 - r2_("mkt", w)).clip(0.0, 1.0).sqrt()
        return feats
