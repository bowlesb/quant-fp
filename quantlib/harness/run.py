"""The harness pipeline: load panel -> walk-forward folds -> train model on each train fold (past) ->
apply the SHARED `CrossSectionalLS` decide-core to the test fold (future, no look-ahead) -> book the
L/S-by-percentile $ P&L net of per-name cost -> percentile-threshold diagnostics + baselines.

THE PORTABILITY INVARIANT (the make-or-break): the scoring the harness ranks on is
`CrossSectionalLS(model=frozen).score(cross_section)` — the EXACT method a live container's `decide`
calls per cycle on a bus `FeatureView`. Here it is applied VECTORIZED (the frozen model predicts the
whole test fold's feature matrix at once, then the per-timestamp top/bottom-k booking is columnar); live
it is applied per single cross-section. The training (walk-forward fit) is offline and produces the
frozen `RankModel` artifact; `tests/harness/test_harness_portability.py` proves the SAME `decide`
applied row-by-row == the vectorized panel apply (parity by construction).

FAST: the model predicts each test fold in one batched call; the booking + diagnostics are columnar
over the resident arrays. Wall-clock is reported.
"""
from __future__ import annotations

import math
import os
import time
from collections import defaultdict

import numpy as np
import polars as pl

from quantlib.backtest import shuffle_within_groups, walk_forward_folds
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
from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.diagnostics import ThresholdCurve, threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.model import CompositeRankModel, GbmRankModel, RidgeRankModel
from quantlib.harness.report import EquityPoint, MoneyResult, StrategyReport, render_summary_md
from quantlib.strategy_core.adapters import PanelCrossSection
from quantlib.strategy_core.cost import long_short_per_name_cost
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS

_MODEL_TRAINERS = {
    ModelKind.GBM: GbmRankModel,
    ModelKind.RIDGE: RidgeRankModel,
    ModelKind.COMPOSITE: CompositeRankModel,
}


def _daily_panel_from_cache(config: HarnessConfig) -> Panel:
    """Build the daily Panel directly from a cached daily-reduce parquet — the dates come from the CACHE
    (not the raw-bars glob), so the harness runs against any cached (symbol, day) panel without the raw
    store mounted. Reuses the battery's `_compute_daily_features` + the SAME liquidity/$1/warmup floors,
    so the panel is identical to `build_daily_panel`'s for the cached range."""
    cached = pl.read_parquet(config.daily_cache)
    daily = cached.filter((pl.col("date") >= config.date_start) & (pl.col("date") <= config.date_end))
    if config.universe_top:
        adv = (
            daily.group_by("symbol")
            .agg(pl.col("rth_dollar_vol").mean().alias("adv"))
            .sort("adv", descending=True)
            .head(config.universe_top)["symbol"]
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


def _load_panel(config: HarnessConfig) -> Panel:
    if config.cadence is Cadence.DAILY:
        if config.daily_cache and os.path.exists(config.daily_cache):
            return _daily_panel_from_cache(config)
        frame = build_daily_panel(
            (config.date_start, config.date_end),
            universe_top=config.universe_top,
            daily_cache=config.daily_cache,
        )
        return panel_from_daily_frame(frame)
    if config.intraday_groups is None:
        raise ValueError("cadence=INTRADAY requires intraday_groups on the config")
    frame = build_intraday_panel(
        (config.date_start, config.date_end),
        feature_groups=config.intraday_groups,
        horizons_min=list(config.intraday_horizons_min),
        universe_top=config.universe_top,
    )
    feature_names = [feat for feats in config.intraday_groups.values() for feat in feats]
    return panel_from_intraday_frame(frame, feature_names)


def _score_test_fold(core: CrossSectionalLS, panel: Panel, test_idx: list[int]) -> np.ndarray:
    """Score the WHOLE test fold by building ONE `PanelCrossSection` over its rows and calling the
    SHARED `core.score` — the exact method a live `decide` calls. The frozen model predicts the whole
    matrix in one batched call (fast); the value is identical to scoring each timestamp separately
    because the model is point-wise per name (rank() is a row-wise map). The parity test pins this."""
    symbols = [str(i) for i in test_idx]  # placeholder names; scoring is per-row, name-agnostic here
    feature_columns = {name: col for col, name in enumerate(panel.feature_names)}
    matrix = panel.feature_matrix[test_idx, :]
    cross_section = PanelCrossSection(symbols, panel.minute_dt[test_idx[0]], matrix, feature_columns)
    return core.score(cross_section)


def _walk_forward_scores(
    config: HarnessConfig, panel: Panel, label: np.ndarray
) -> tuple[list[float], list[float], list, list[str], list[float]]:
    """Train a model on each expanding train fold (purged by the label horizon), apply the SHARED
    decide-core's `score` to the test fold. Returns the flat (score, label, timestamp, symbol, spread)
    arrays the booking + diagnostics consume — only OOS test-fold rows (no look-ahead)."""
    row_ts = panel.minute_dt
    folds = walk_forward_folds(row_ts, config.label_horizon_minutes, config.n_folds)
    trainer = _MODEL_TRAINERS[config.model]

    scores: list[float] = []
    labels: list[float] = []
    groups: list = []
    symbols: list[str] = []
    spreads: list[float] = []
    for fold in folds:
        train_idx = [i for i in fold.train_idx if np.isfinite(label[i])]
        test_idx = fold.test_idx
        if len(train_idx) < config.min_train_rows or len(test_idx) < config.min_test_rows:
            continue
        train_x = panel.feature_matrix[train_idx, :]
        train_y = label[train_idx]
        model = trainer.train(train_x, train_y, panel.feature_names)
        core = CrossSectionalLS(frac=config.long_short_frac, model=model)
        fold_scores = _score_test_fold(core, panel, test_idx)
        for position, i in enumerate(test_idx):
            scores.append(float(fold_scores[position]))
            labels.append(float(label[i]))
            groups.append(row_ts[i])
            symbols.append(panel.symbol_names[int(panel.symbol_code[i])])
            spreads.append(float(panel.half_spread_bps[i]))
    return scores, labels, groups, symbols, spreads


def _money_from_basket(
    scores: list[float],
    labels: list[float],
    groups: list,
    symbols: list[str],
    spreads: list[float],
    config: HarnessConfig,
) -> MoneyResult:
    """Book the configured-frac L/S basket through the SHARED per-name-cost model and roll it into the
    $ equity curve on the book capital."""
    cost_spreads = [spread + config.slippage_bps for spread in spreads]
    economics = long_short_per_name_cost(
        scores,
        labels,
        groups,
        symbols,
        cost_spreads,
        frac=config.long_short_frac,
        cost_mult=config.cost_mult,
        borrow_bps_annual=config.borrow_bps_annual,
        periods_per_year=config.periods_per_year,
    )
    per_period = _per_period_net(scores, labels, groups, symbols, cost_spreads, config)
    equity_curve, total_pnl, max_dd, net_return = _equity(per_period, config.capital)
    finite_spreads = [s for s in cost_spreads if s == s]
    return MoneyResult(
        capital=config.capital,
        total_pnl=total_pnl,
        net_return=net_return,
        sharpe_net=float(economics.get("sharpe_net", float("nan"))),
        max_drawdown=max_dd,
        mean_turnover=float(economics.get("mean_turnover", float("nan"))),
        n_periods=int(economics.get("n_periods", 0)),
        breakeven_cost_bps=float(economics.get("breakeven_cost_bps", float("nan"))),
        cost_used_bps=float(np.median(finite_spreads)) if finite_spreads else float("nan"),
        equity_curve=equity_curve,
    )


def _per_period_net(
    scores: list[float],
    labels: list[float],
    groups: list,
    symbols: list[str],
    cost_spreads: list[float],
    config: HarnessConfig,
) -> list[tuple[object, float]]:
    """Per-timestamp net return of the configured-frac basket (the equity-curve increments). Reuses the
    same bucketing/cost logic as `long_short_per_name_cost` so the equity curve == the headline P&L."""
    buckets: dict[object, list[tuple[float, float, str, float]]] = defaultdict(list)
    for score, label, group, symbol, spread in zip(scores, labels, groups, symbols, cost_spreads):
        if not (math.isnan(score) or math.isnan(label)):
            buckets[group].append((score, label, symbol, spread if spread == spread else 0.0))
    borrow_per_period = (config.borrow_bps_annual / 1e4) / config.periods_per_year
    prev_w: dict[str, float] = {}
    series: list[tuple[object, float]] = []
    for ts in sorted(buckets):
        rows = sorted(buckets[ts], key=lambda row: row[0])
        k = max(1, int(config.long_short_frac * len(rows)))
        if len(rows) < 2 * k:
            continue
        shorts, longs = rows[:k], rows[-k:]
        weights: dict[str, float] = {}
        spread_by_sym: dict[str, float] = {}
        for _, _, sym, spread in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
            spread_by_sym[sym] = spread
        for _, _, sym, spread in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
            spread_by_sym[sym] = spread
        gross = sum(weights[sym] * ret for _, ret, sym, _ in longs + shorts)
        cost = 0.0
        for sym in set(weights) | set(prev_w):
            dw = abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            cost += spread_by_sym.get(sym, 0.0) * config.cost_mult / 1e4 * dw
        series.append((ts, gross - cost - borrow_per_period))
        prev_w = weights
    return series


def _equity(
    per_period: list[tuple[object, float]], capital: float
) -> tuple[list[EquityPoint], float, float, float]:
    equity = capital
    peak = capital
    max_dd = 0.0
    points: list[EquityPoint] = []
    for period, (ts, net) in enumerate(per_period):
        equity += net * capital
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        points.append(EquityPoint(period=period, timestamp=str(ts), net_return=net, equity=equity))
    total_pnl = equity - capital
    net_return = total_pnl / capital if capital else float("nan")
    return points, total_pnl, max_dd, net_return


def _baseline_curves(
    scores: list[float],
    labels: list[float],
    groups: list,
    symbols: list[str],
    spreads: list[float],
    config: HarnessConfig,
) -> tuple[ThresholdCurve | None, float]:
    shuffle_curve: ThresholdCurve | None = None
    if config.run_shuffle_baseline:
        shuffled = shuffle_within_groups(labels, groups, config.seed)
        shuffle_curve = threshold_curve(
            scores,
            shuffled,
            groups,
            symbols,
            spreads,
            cuts=config.percentile_cuts,
            capital=config.capital,
            cost_mult=config.cost_mult,
            slippage_bps=config.slippage_bps,
            borrow_bps_annual=config.borrow_bps_annual,
            periods_per_year=config.periods_per_year,
        )
    predict_zero_pnl = 0.0  # a no-signal book trades nothing -> $0 P&L (the trivial null)
    return shuffle_curve, predict_zero_pnl


def run_strategy(config: HarnessConfig) -> StrategyReport:
    """The one entry point: train -> apply -> evaluate, returning the organized `StrategyReport`."""
    t0 = time.perf_counter()
    panel = _load_panel(config)
    label = forward_excess_label(
        panel, horizon_days=config.label_horizon_days, horizon_min=config.label_horizon_min
    )
    t_panel = time.perf_counter()

    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    notes: list[str] = []
    if not scores:
        notes.append("no OOS scores produced (too few folds/rows) — check date range / universe")

    curve = threshold_curve(
        scores,
        labels,
        groups,
        symbols,
        spreads,
        cuts=config.percentile_cuts,
        capital=config.capital,
        cost_mult=config.cost_mult,
        slippage_bps=config.slippage_bps,
        borrow_bps_annual=config.borrow_bps_annual,
        periods_per_year=config.periods_per_year,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    shuffle_curve, predict_zero_pnl = _baseline_curves(scores, labels, groups, symbols, spreads, config)
    t_eval = time.perf_counter()

    report = StrategyReport(
        config=config,
        n_rows=panel.n_rows,
        n_features=len(panel.feature_names),
        n_symbols=len(panel.symbol_names),
        n_test_timestamps=len({str(g) for g in groups}),
        money=money,
        threshold_curve=curve,
        shuffle_curve=shuffle_curve,
        predict_zero_total_pnl=predict_zero_pnl,
        panel_load_seconds=round(t_panel - t0, 3),
        fit_apply_seconds=round(t_eval - t_panel, 3),
        total_seconds=round(t_eval - t0, 3),
        notes=notes,
    )
    report.summary_md = render_summary_md(report)
    return report
