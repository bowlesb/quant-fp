"""`run_battery(config)` — the SINGLE entrypoint Ben asked for.

Load the feature matrix ONCE, run a declared RANGE of strategies over the SAME resident arrays
EXTREMELY quickly (no per-strategy panel rebuild), each with anti-fooling baselines built in, and report
the measured wall-time. Includes per-minute LOOK-AHEAD label strategies (triple-barrier / forward
run-up), vectorized across all dataset minutes.

The flow:

    panel = load_panel(config.data)              # ONCE — the shared feature matrix
    for strategy in config.strategies:           # each is a StrategyConfig (one config line)
        label  = build_label(panel, strategy)    # forward-excess OR a per-minute look-ahead label
        result = evaluate(panel, label, strategy) # walk-forward score -> net L/S P&L + shuffle/zero nulls

The scoring path is the SHARED `CrossSectionalLS.score` (the exact method a live container's decide()
calls), applied vectorized over each test fold — backtest==live by construction.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

from quantlib.backtest import (
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)
from quantlib.battery.battery_config import (
    BatteryConfig,
    Cadence,
    DataSpec,
    LabelKind,
    SignalKind,
    StrategyConfig,
)
from quantlib.battery.battery_report import BatteryReport, StrategyResult
from quantlib.battery.lookahead import fwd_max_runup_label, up_move_start_label
from quantlib.battery.panel import (
    MIN_DOLLAR_VOL,
    MIN_PRICE,
    MIN_TRAILING_DAYS,
    Panel,
    _compute_daily_features,
    build_daily_panel,
    build_intraday_panel,
    panel_from_daily_frame,
    panel_from_intraday_frame,
)
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.model import CompositeRankModel, GbmRankModel, RidgeRankModel
from quantlib.strategy_core.adapters import PanelCrossSection
from quantlib.strategy_core.cost import long_short_per_name_cost
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS

import polars as pl

_TRAINERS = {
    SignalKind.GBM: GbmRankModel,
    SignalKind.RIDGE: RidgeRankModel,
    SignalKind.COMPOSITE: CompositeRankModel,
}


# ---------------------------------------------------------------------------
# Panel load — ONCE, shared across every strategy in the battery
# ---------------------------------------------------------------------------


def load_panel(data: DataSpec) -> Panel:
    """Build the shared feature-matrix Panel for the whole battery. Daily reuses the cached raw-bar
    reduce when present (so it runs without the raw store mounted); intraday joins the store groups."""
    if data.cadence is Cadence.DAILY:
        if data.daily_cache and os.path.exists(data.daily_cache):
            return _daily_panel_from_cache(data)
        frame = build_daily_panel(
            (data.date_start, data.date_end),
            universe_top=data.universe_top,
            daily_cache=data.daily_cache,
        )
        return panel_from_daily_frame(frame)
    if data.intraday_groups is None:
        raise ValueError("cadence=INTRADAY requires intraday_groups on the DataSpec")
    frame = build_intraday_panel(
        (data.date_start, data.date_end),
        feature_groups=data.intraday_groups,
        horizons_min=list(data.intraday_horizons_min),
        universe_top=data.universe_top,
    )
    feature_names = [feat for feats in data.intraday_groups.values() for feat in feats]
    return panel_from_intraday_frame(frame, feature_names)


def _daily_panel_from_cache(data: DataSpec) -> Panel:
    """Build the daily Panel from the cached daily-reduce parquet (dates come from the CACHE), reusing
    the battery's feature/liquidity/$1/warmup floors so it is identical to `build_daily_panel`'s."""
    cached = pl.read_parquet(data.daily_cache)
    daily = cached.filter((pl.col("date") >= data.date_start) & (pl.col("date") <= data.date_end))
    if data.universe_top:
        adv = (
            daily.group_by("symbol")
            .agg(pl.col("rth_dollar_vol").mean().alias("adv"))
            .sort("adv", descending=True)
            .head(data.universe_top)["symbol"]
        )
        daily = daily.filter(pl.col("symbol").is_in(adv))
    feat = _compute_daily_features(daily)
    feat = feat.filter(
        (pl.col("bar_idx") >= MIN_TRAILING_DAYS)
        & ((pl.col("rth_close") * pl.col("rth_volume")) >= MIN_DOLLAR_VOL)
        & (pl.col("rth_close") >= MIN_PRICE)
    )
    feat = feat.with_columns(
        (
            pl.col("date").str.to_datetime("%Y-%m-%d", time_zone="UTC") + pl.duration(hours=19, minutes=59)
        ).alias("minute")
    )
    return panel_from_daily_frame(feat)


# ---------------------------------------------------------------------------
# Label — forward-excess OR a per-minute look-ahead label
# ---------------------------------------------------------------------------


def build_label(panel: Panel, config: BatteryConfig, strategy: StrategyConfig) -> np.ndarray:
    """The forward outcome this strategy is graded against. FORWARD_EXCESS reuses the harness's
    gap-safe forward-excess; the LOOK-AHEAD kinds call the vectorized per-minute label primitives."""
    if strategy.label is LabelKind.FORWARD_EXCESS:
        if panel.cadence == "daily":
            return forward_excess_label(panel, horizon_days=strategy.horizon, horizon_min=0)
        return forward_excess_label(panel, horizon_days=0, horizon_min=strategy.horizon)
    if strategy.label is LabelKind.UP_MOVE_START:
        return up_move_start_label(panel, strategy.horizon, strategy.barrier_bps)
    if strategy.label is LabelKind.FWD_MAX_RUNUP:
        return fwd_max_runup_label(panel, strategy.horizon)
    raise ValueError(f"unknown label kind {strategy.label}")


# ---------------------------------------------------------------------------
# Feature-subset resolution
# ---------------------------------------------------------------------------


def _feature_subset(panel: Panel, strategy: StrategyConfig) -> tuple[np.ndarray, list[str], list[int]]:
    """The (matrix, names, column indices) for this strategy's declared feature subset (or the whole
    matrix when `features` is None). Raises if a named feature is absent — fail loud, no silent drop."""
    if not strategy.features:
        cols = list(range(len(panel.feature_names)))
        return panel.feature_matrix, list(panel.feature_names), cols
    missing = [name for name in strategy.features if name not in panel.feature_names]
    if missing:
        raise KeyError(f"strategy {strategy.name!r} requests absent features {missing}")
    cols = [panel.feature_names.index(name) for name in strategy.features]
    return panel.feature_matrix[:, cols], list(strategy.features), cols


# ---------------------------------------------------------------------------
# Evaluate ONE strategy over the shared panel
# ---------------------------------------------------------------------------


def evaluate_strategy(panel: Panel, config: BatteryConfig, strategy: StrategyConfig) -> StrategyResult:
    """Walk-forward score -> net L/S P&L + the shuffle / predict-zero nulls, for ONE strategy over the
    SHARED panel arrays. No store read, no panel rebuild — the per-strategy cost is just the fit+apply."""
    label = build_label(panel, config, strategy)
    sub_matrix, sub_names, _ = _feature_subset(panel, strategy)
    horizon_min = config.horizon_minutes(strategy)
    ppy = config.periods_per_year(strategy)

    row_ts = panel.minute_dt
    folds = walk_forward_folds(row_ts, horizon_min, config.n_folds)

    scores: list[float] = []
    labels: list[float] = []
    groups: list = []
    symbols: list[str] = []
    spreads: list[float] = []
    for fold in folds:
        train_idx = [i for i in fold.train_idx if np.isfinite(label[i])]
        test_idx = list(fold.test_idx)
        if len(train_idx) < config.min_train_rows or len(test_idx) < config.min_test_rows:
            continue
        core = _fit_core(strategy, sub_matrix, sub_names, label, train_idx)
        fold_scores = _score_fold(core, sub_matrix, sub_names, panel.minute_dt[test_idx[0]], test_idx)
        for position, i in enumerate(test_idx):
            scores.append(float(fold_scores[position]))
            labels.append(float(label[i]))
            groups.append(row_ts[i])
            symbols.append(panel.symbol_names[int(panel.symbol_code[i])])
            spreads.append(float(panel.half_spread_bps[i]))

    return _grade(strategy, config, scores, labels, groups, symbols, spreads, horizon_min, ppy)


def _fit_core(
    strategy: StrategyConfig,
    sub_matrix: np.ndarray,
    sub_names: list[str],
    label: np.ndarray,
    train_idx: list[int],
) -> CrossSectionalLS:
    """Build the SHARED `CrossSectionalLS` decide-core for this strategy. FEATURE ranks one signed named
    feature directly (no fit); COMPOSITE/RIDGE/GBM fit a frozen RankModel on the train fold."""
    if strategy.signal is SignalKind.FEATURE:
        feature = strategy.signal_feature or sub_names[0]
        if feature not in sub_names:
            raise KeyError(f"signal_feature {feature!r} not in strategy {strategy.name!r} features")
        return CrossSectionalLS(frac=strategy.frac, signal_feature=feature, signal_sign=strategy.signal_sign)
    trainer = _TRAINERS[strategy.signal]
    train_x = sub_matrix[train_idx, :]
    train_y = label[train_idx]
    model = trainer.train(train_x, train_y, sub_names)
    return CrossSectionalLS(frac=strategy.frac, model=model)


def _score_fold(
    core: CrossSectionalLS,
    sub_matrix: np.ndarray,
    sub_names: list[str],
    first_ts: object,
    test_idx: list[int],
) -> np.ndarray:
    """Score the whole test fold via ONE `PanelCrossSection` over its rows + the SHARED `core.score`
    (the exact per-cycle live-decide method, applied vectorized). Name-agnostic per-row scoring."""
    feature_columns = {name: col for col, name in enumerate(sub_names)}
    matrix = sub_matrix[test_idx, :]
    symbols = [str(i) for i in test_idx]
    cross_section = PanelCrossSection(symbols, first_ts, matrix, feature_columns)
    return np.asarray(core.score(cross_section), dtype=float)


# ---------------------------------------------------------------------------
# Grade — net economics + the two anti-fooling nulls
# ---------------------------------------------------------------------------


def _grade(
    strategy: StrategyConfig,
    config: BatteryConfig,
    scores: list[float],
    labels: list[float],
    groups: list,
    symbols: list[str],
    spreads: list[float],
    horizon_min: int,
    ppy: float,
) -> StrategyResult:
    if not scores:
        return StrategyResult(
            name=strategy.name,
            signal=strategy.signal.value,
            label=strategy.label.value,
            horizon=strategy.horizon,
            n_test_ts=0,
            n_rows=0,
            mean_ic=float("nan"),
            shuffle_ic=float("nan"),
            edge_vs_shuffle=float("nan"),
            nw_t=float("nan"),
            net_per_period=float("nan"),
            gross_per_period=float("nan"),
            sharpe_net=float("nan"),
            breakeven_cost_bps=float("nan"),
            cost_used_bps=float("nan"),
            predict_zero_pnl=0.0,
            notes="no OOS scores (too few folds/rows)",
        )
    cost_spreads = [s + config.slippage_bps for s in spreads]
    economics = long_short_per_name_cost(
        scores,
        labels,
        groups,
        symbols,
        cost_spreads,
        frac=strategy.frac,
        cost_mult=config.cost_mult,
        borrow_bps_annual=config.borrow_bps_annual,
        periods_per_year=ppy,
    )
    real_ic = per_timestamp_ic(scores, labels, groups, min_names=20)
    mean_real = mean_ic(real_ic)
    lag = max(1, horizon_min // (390 if config.data.cadence is Cadence.DAILY else 30))
    nw_t = newey_west_tstat(real_ic, lag)
    shuffle_ic_mean = float("nan")
    if config.run_shuffle_baseline:
        shuffled = shuffle_within_groups(labels, groups, config.seed)
        shuffle_ic_mean = mean_ic(per_timestamp_ic(scores, shuffled, groups, min_names=20))
    finite_spreads = [s for s in cost_spreads if s == s]
    return StrategyResult(
        name=strategy.name,
        signal=strategy.signal.value,
        label=strategy.label.value,
        horizon=strategy.horizon,
        n_test_ts=len({str(g) for g in groups}),
        n_rows=len(labels),
        mean_ic=mean_real,
        shuffle_ic=shuffle_ic_mean,
        edge_vs_shuffle=(mean_real - shuffle_ic_mean) if not math.isnan(mean_real) else float("nan"),
        nw_t=float(nw_t),
        net_per_period=float(economics.get("net_per_period", float("nan"))),
        gross_per_period=float(economics.get("gross_per_period", float("nan"))),
        sharpe_net=float(economics.get("sharpe_net", float("nan"))),
        breakeven_cost_bps=float(economics.get("breakeven_cost_bps", float("nan"))),
        cost_used_bps=float(np.median(finite_spreads)) if finite_spreads else float("nan"),
        predict_zero_pnl=0.0 if config.run_predict_zero_baseline else float("nan"),
        notes="",
    )


# ---------------------------------------------------------------------------
# The single entrypoint
# ---------------------------------------------------------------------------


def run_battery(config: BatteryConfig) -> BatteryReport:
    """Load the feature matrix ONCE, evaluate every `StrategyConfig` over the shared arrays, and return
    the BatteryReport with per-strategy results + the measured wall-time (panel-load vs eval)."""
    t0 = time.perf_counter()
    panel = load_panel(config.data)
    t_panel = time.perf_counter()

    results: list[StrategyResult] = []
    for strategy in config.strategies:
        results.append(evaluate_strategy(panel, config, strategy))
    t_eval = time.perf_counter()

    report = BatteryReport(
        cadence=config.data.cadence.value,
        date_range=(config.data.date_start, config.data.date_end),
        universe_top=config.data.universe_top,
        n_rows=panel.n_rows,
        n_features=len(panel.feature_names),
        n_symbols=len(panel.symbol_names),
        results=results,
        panel_load_seconds=round(t_panel - t0, 3),
        eval_seconds=round(t_eval - t_panel, 3),
        total_seconds=round(t_eval - t0, 3),
    )
    return report
