"""The declarative harness config — "just run it" with sane defaults, every knob overridable.

A `HarnessConfig` is a pure record (no I/O); `run_strategy` consumes it. The defaults run the demo:
the cached daily panel, a forward-1-day cross-sectional excess-return label, a gradient-boosted-tree
ranker, walk-forward with horizon-length purge, a 10% top/bottom L/S basket on $1,000,000.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModelKind(str, Enum):
    """The trainable ranker. GBM = LightGBM gradient-boosted trees (robust default, NaN-tolerant);
    RIDGE = a regularized linear model (fast, interpretable); COMPOSITE = the no-fit equal-weight
    z-score composite (the battery fast-path screen, no training)."""

    GBM = "gbm"
    RIDGE = "ridge"
    COMPOSITE = "composite"


class Cadence(str, Enum):
    DAILY = "daily"
    INTRADAY = "intraday"


@dataclass(frozen=True)
class HarnessConfig:
    """Everything `run_strategy` needs, declaratively.

    DATA: a (tickers x features x time) panel. The daily path reuses the battery `build_daily_panel`
    (cached raw-bar reduce via `daily_cache`); the intraday path reuses `build_intraday_panel`. The
    forward-return LABEL is derived on the panel at `label_horizon_days` (daily) / `label_horizon_min`
    (intraday).

    SPLIT: expanding walk-forward — `n_folds` test blocks, each PURGED by the label horizon so no
    training label peeks into its test block (look-ahead defense).

    STRATEGY: long top-`long_short_frac`, short bottom-`long_short_frac` of model scores, equal-$,
    dollar-neutral. `capital` is the book size the $ P&L is reported on.

    COST: per-name half-spread (from the panel's `half_spread_bps`) x `cost_mult` + `slippage_bps`
    + `borrow_bps_annual` on the short leg.
    """

    # --- data ---
    cadence: Cadence = Cadence.DAILY
    daily_cache: str | None = "experiments/data/battery_daily_cache.parquet"
    date_start: str = "2025-12-01"
    date_end: str = "2026-06-17"
    universe_top: int | None = 500  # cap to the most-liquid N names (the only tradeable universe)
    intraday_groups: dict[str, list[str]] | None = None  # required for cadence=INTRADAY
    intraday_horizons_min: tuple[int, ...] = (30, 60)

    # --- label ---
    label_horizon_days: int = 1  # daily cadence: forward k-trading-day cross-sectional excess return
    label_horizon_min: int = 30  # intraday cadence: forward k-minute excess return

    # --- model ---
    model: ModelKind = ModelKind.GBM
    n_folds: int = 5
    min_train_rows: int = 500
    min_test_rows: int = 50
    seed: int = 13

    # --- strategy / sizing ---
    long_short_frac: float = 0.10
    capital: float = 1_000_000.0

    # --- cost ---
    cost_mult: float = 1.0
    slippage_bps: float = 1.0
    borrow_bps_annual: float = 50.0

    # --- diagnostics ---
    # the percentile cuts the threshold-curve reports (top/bottom X% each). Ben's "conservative
    # thresholds": as the cut shrinks, does $/trade + precision improve?
    percentile_cuts: tuple[float, ...] = (0.01, 0.02, 0.05, 0.10, 0.20, 0.33, 0.50)

    # --- baselines ---
    run_shuffle_baseline: bool = True  # within-timestamp label shuffle (the leakage/overfit null)
    run_predict_zero_baseline: bool = True  # no-signal book == 0 $

    @property
    def label_horizon_minutes(self) -> int:
        """The label horizon in MARKET minutes — what the walk-forward purge uses. Daily cadence: one
        trading day == 390 RTH minutes, so k days == k*390 (the purge is in market time, not bar
        count, matching `walk_forward_folds`)."""
        if self.cadence is Cadence.DAILY:
            return self.label_horizon_days * 390
        return self.label_horizon_min

    @property
    def periods_per_year(self) -> float:
        """Rebalance periods per year, for annualizing the Sharpe. Daily cadence rebalances once per
        trading day (~252); intraday once per sampled minute per day."""
        if self.cadence is Cadence.DAILY:
            return 252.0 / max(1, self.label_horizon_days)
        # intraday: 13 tradeable samples/day x 252 days, scaled by the holding horizon in samples
        samples_per_day = 13.0
        return 252.0 * samples_per_day / max(1.0, self.label_horizon_min / 30.0)
