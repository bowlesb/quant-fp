"""The declarative battery config — Ben's "SINGLE script + a CONFIGURATION abstraction that runs a
RANGE of strategies over ONE feature matrix EXTREMELY quickly, including per-minute LOOK-AHEAD".

A `BatteryConfig` = one shared DATA spec (the feature matrix, loaded ONCE) + a LIST of `StrategyConfig`
entries. Adding a strategy is adding ONE `StrategyConfig` to the list — never writing a new bespoke
experiment script. Each `StrategyConfig` is the full point in the strategy space:

    (feature subset)  x  (signal rule)  x  (label / horizon)  x  (entry)  x  (sizing)

`run_battery(config)` loads the shared panel once and evaluates every `StrategyConfig` over the SAME
resident arrays (no per-strategy panel rebuild — the bulk of "extremely fast"), each with the anti-fooling
baselines (shuffle + predict-zero) built in, and reports the measured wall-time.

These are pure records (no I/O). `run_battery` consumes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Cadence(str, Enum):
    DAILY = "daily"
    INTRADAY = "intraday"


class SignalKind(str, Enum):
    """How a `StrategyConfig` turns its feature subset into the per-name conviction the L/S basket ranks.

    FEATURE   — rank ONE named feature directly (x `signal_sign`); the raw fast path, exact backtest==live
                parity, no model fit. This is the single-feature signed-decile signal the hand-rolled
                weekly-reversal / G0 screens used — now a config line.
    COMPOSITE — the no-fit equal-weight z-score composite of the whole feature subset (the honest
                "does ANY equal-weight combination rank?" screen).
    RIDGE     — a regularized linear combiner fit walk-forward over the subset.
    GBM       — a LightGBM non-linear combiner fit walk-forward over the subset (the deeper mode).
    """

    FEATURE = "feature"
    COMPOSITE = "composite"
    RIDGE = "ridge"
    GBM = "gbm"


class LabelKind(str, Enum):
    """What forward outcome each row is graded against — INCLUDING the per-minute look-ahead kinds.

    FORWARD_EXCESS  — forward cross-sectional EXCESS return at the horizon (the L/S basket's natural
                      target; "did this name out/under-perform its peers over the next H?"). The default.
    UP_MOVE_START   — LOOK-AHEAD per minute: is THIS minute the start of an up-move over the next H bars?
                      A triple-barrier first-touch label: +1 if the forward path hits +`barrier` before
                      -`barrier` within H bars, -1 if it hits -`barrier` first, 0 if neither (timeout).
                      Vectorized across ALL minutes of the dataset.
    FWD_MAX_RUNUP   — LOOK-AHEAD per minute: the maximum forward run-up (max high over the next H bars /
                      entry - 1), a continuous magnitude target. Vectorized forward-window extremum.
    """

    FORWARD_EXCESS = "forward_excess"
    UP_MOVE_START = "up_move_start"
    FWD_MAX_RUNUP = "fwd_max_runup"


@dataclass(frozen=True)
class DataSpec:
    """The shared feature matrix — loaded ONCE, shared across every strategy in the battery.

    DAILY: the daily-reduced (symbol, trading-day) panel built from the cached raw-bar reduce
    (`daily_cache`) or the raw store; features = the trailing-EOD `DAILY_FEATURE_COLS`.
    INTRADAY: the (symbol, sampled-minute) store panel; `intraday_groups` = {store group: [features]}
    joined point-in-time, with forward labels at `intraday_horizons_min`.
    """

    cadence: Cadence = Cadence.DAILY
    date_start: str = "2025-12-01"
    date_end: str = "2026-06-17"
    universe_top: int | None = 500
    daily_cache: str | None = "experiments/data/battery_daily_cache.parquet"
    intraday_groups: dict[str, list[str]] | None = None
    intraday_horizons_min: tuple[int, ...] = (30, 60)


@dataclass(frozen=True)
class StrategyConfig:
    """ONE strategy in the battery — the full configurable point. Adding a strategy = adding one of these.

    name           — a label for the report row.
    features       — the feature subset this strategy uses (subset of the panel's columns). Empty/None =
                     ALL panel features (the whole-matrix screen).
    signal         — how `features` becomes the ranked conviction (FEATURE/COMPOSITE/RIDGE/GBM).
    signal_feature — the single feature to rank when `signal=FEATURE` (must be one of `features`).
    signal_sign    — +1 ranks high feature -> LONG; -1 inverts (a reversion signal). FEATURE signal only.
    label          — the forward outcome graded against (FORWARD_EXCESS or a per-minute LOOK-AHEAD kind).
    horizon        — the forward horizon: trading DAYS (daily cadence) or MINUTES (intraday cadence).
    barrier_bps    — the +/- barrier for the UP_MOVE_START triple-barrier label (bps; ignored otherwise).
    frac           — top/bottom fraction for the EW dollar-neutral L/S basket.
    """

    name: str
    signal: SignalKind = SignalKind.COMPOSITE
    label: LabelKind = LabelKind.FORWARD_EXCESS
    horizon: int = 1
    features: tuple[str, ...] | None = None
    signal_feature: str | None = None
    signal_sign: float = 1.0
    barrier_bps: float = 50.0
    frac: float = 0.10


@dataclass(frozen=True)
class BatteryConfig:
    """A full battery: one shared `DataSpec` + a LIST of `StrategyConfig`s, plus the shared knobs every
    strategy is booked and graded under (the cost model + the walk-forward + the baselines)."""

    data: DataSpec
    strategies: list[StrategyConfig] = field(default_factory=list)

    # --- shared walk-forward ---
    n_folds: int = 5
    min_train_rows: int = 500
    min_test_rows: int = 50
    seed: int = 13

    # --- shared sizing / cost ---
    capital: float = 1_000_000.0
    cost_mult: float = 1.0
    slippage_bps: float = 1.0
    borrow_bps_annual: float = 50.0

    # --- shared diagnostics / baselines ---
    percentile_cuts: tuple[float, ...] = (0.01, 0.02, 0.05, 0.10, 0.20)
    run_shuffle_baseline: bool = True
    run_predict_zero_baseline: bool = True

    def horizon_minutes(self, strategy: StrategyConfig) -> int:
        """The strategy's horizon in MARKET minutes (the walk-forward purge unit). Daily: H days x 390
        RTH minutes; intraday: H minutes directly."""
        if self.data.cadence is Cadence.DAILY:
            return strategy.horizon * 390
        return strategy.horizon

    def periods_per_year(self, strategy: StrategyConfig) -> float:
        """Rebalance periods/year for annualizing this strategy's Sharpe."""
        if self.data.cadence is Cadence.DAILY:
            return 252.0 / max(1, strategy.horizon)
        samples_per_day = 13.0
        return 252.0 * samples_per_day / max(1.0, strategy.horizon / 30.0)
