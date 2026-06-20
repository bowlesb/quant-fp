"""Pipeline-level harness tests: the PLANTED-SIGNAL sanity check (the harness detects $ edge when edge
exists), the look-ahead / discipline guards, and the threshold-curve / label unit tests.

The planted test is the proof the demonstrated numbers are MEANINGFUL: on a synthetic panel where a
feature genuinely predicts the forward return, the harness must show high precision, positive $ P&L, and
a real curve that DOMINATES the shuffle baseline. On a no-signal panel it must collapse to ~chance.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.backtest import shuffle_within_groups
from quantlib.battery.panel import Panel
from quantlib.harness.config import HarnessConfig, ModelKind
from quantlib.harness.diagnostics import threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.run import _money_from_basket, _walk_forward_scores

CUTS = (0.05, 0.10, 0.20)


def _synthetic_panel(n_days: int, n_names: int, signal_strength: float, seed: int) -> Panel:
    """A daily-shaped panel: `n_names` symbols over `n_days`. Feature f0 predicts the next-day return
    with `signal_strength` (0 == pure noise). The panel carries one feature column + the execution
    prices the label reads, all internally consistent (forward close encodes the planted return)."""
    rng = np.random.default_rng(seed)
    base = dt.datetime(2026, 1, 1, 19, 59, tzinfo=dt.timezone.utc)
    symbol_code = np.repeat(np.arange(n_names), n_days)
    minute_epoch = np.tile(
        np.array([int((base + dt.timedelta(days=d)).timestamp() * 1e9) for d in range(n_days)]),
        n_names,
    ).astype(np.int64)
    n = n_names * n_days
    feature = rng.normal(0, 1, n)
    entry = np.full(n, 100.0)
    # The harness label for row t reads close[t+1]/entry[t]-1 (the forward return). To plant an edge,
    # the NEXT day's close must be driven by THIS row's feature: close[t+1] = 100*(1 + signal*f[t] +
    # noise), shifted forward WITHIN each symbol block. close[block_start] is unconstrained (no row
    # predicts it); the last row of each block has no forward and yields a NaN label.
    target_return = signal_strength * feature + rng.normal(0, 0.02, n)
    close = entry.copy()
    for symbol_idx in range(n_names):
        block = slice(symbol_idx * n_days, (symbol_idx + 1) * n_days)
        block_close = entry[block].copy()
        block_close[1:] = 100.0 * (1.0 + target_return[block][:-1])
        close[block] = block_close
    return Panel(
        symbol_code=symbol_code,
        symbol_names=[f"S{i}" for i in range(n_names)],
        minute_epoch=minute_epoch,
        feature_names=["f0"],
        feature_matrix=feature.reshape(-1, 1),
        entry_close=entry,
        half_spread_bps=np.full(n, 2.0),
        high=close,
        low=close,
        volume=np.full(n, 1e7),
        extra={"rth_close": close, "rth_dollar_vol": np.full(n, 1e7)},
        cadence="daily",
    )


def _scores_for(panel: Panel, model: ModelKind, n_folds: int = 4) -> tuple:
    config = HarnessConfig(
        model=model,
        n_folds=n_folds,
        long_short_frac=0.1,
        min_train_rows=100,
        min_test_rows=20,
        daily_cache=None,
        percentile_cuts=CUTS,
    )
    label = forward_excess_label(panel, horizon_days=1, horizon_min=30)
    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    return config, scores, labels, groups, symbols, spreads


def test_planted_signal_is_detected() -> None:
    """A strong planted edge -> high precision, positive $ P&L, real curve dominates shuffle."""
    panel = _synthetic_panel(n_days=120, n_names=80, signal_strength=0.05, seed=1)
    config, scores, labels, groups, symbols, spreads = _scores_for(panel, ModelKind.GBM)
    assert scores, "expected OOS scores on the synthetic panel"

    curve = threshold_curve(
        scores,
        labels,
        groups,
        symbols,
        spreads,
        cuts=CUTS,
        capital=1_000_000.0,
        cost_mult=1.0,
        slippage_bps=1.0,
        borrow_bps_annual=50.0,
        periods_per_year=252.0,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)

    # the model has real skill
    assert curve.auc > 0.6, f"planted signal should give AUC>0.6, got {curve.auc}"
    assert curve.rank_ic > 0.1, f"planted signal should give IC>0.1, got {curve.rank_ic}"
    # money is positive
    assert money.total_pnl > 0, f"planted signal should make money, got {money.total_pnl}"
    # the tightest cut has the best precision (conservative-application signature)
    precisions = [cut.directional_precision for cut in curve.cuts]
    assert precisions[0] >= precisions[-1], "precision should not worsen as the cut tightens"
    assert precisions[0] > 0.55

    # the real curve dominates the shuffle baseline
    shuffled = shuffle_within_groups(labels, groups, config.seed)
    shuffle_curve = threshold_curve(
        scores,
        shuffled,
        groups,
        symbols,
        spreads,
        cuts=CUTS,
        capital=1_000_000.0,
        cost_mult=1.0,
        slippage_bps=1.0,
        borrow_bps_annual=50.0,
        periods_per_year=252.0,
    )
    assert curve.cuts[0].total_dollar_pnl > shuffle_curve.cuts[0].total_dollar_pnl
    assert abs(shuffle_curve.auc - 0.5) < 0.05, "shuffle AUC should be ~0.5"


def test_no_signal_collapses_to_chance() -> None:
    """A pure-noise panel -> AUC ~0.5, IC ~0 (the harness does not manufacture a false edge)."""
    panel = _synthetic_panel(n_days=120, n_names=80, signal_strength=0.0, seed=2)
    _config, scores, labels, groups, symbols, spreads = _scores_for(panel, ModelKind.GBM)
    curve = threshold_curve(
        scores,
        labels,
        groups,
        symbols,
        spreads,
        cuts=CUTS,
        capital=1_000_000.0,
        cost_mult=1.0,
        slippage_bps=1.0,
        borrow_bps_annual=50.0,
        periods_per_year=252.0,
    )
    assert abs(curve.auc - 0.5) < 0.06, f"no-signal AUC should be ~0.5, got {curve.auc}"
    assert abs(curve.rank_ic) < 0.06, f"no-signal IC should be ~0, got {curve.rank_ic}"


def test_label_is_forward_only_no_lookahead() -> None:
    """The label for a row uses only its FORWARD price; the last day of each symbol block has no
    forward -> NaN (cannot leak a future it doesn't have)."""
    panel = _synthetic_panel(n_days=10, n_names=5, signal_strength=0.05, seed=4)
    label = forward_excess_label(panel, horizon_days=1, horizon_min=30)
    # the last row of each symbol block (day index 9) must be NaN
    for symbol_idx in range(5):
        last_row = symbol_idx * 10 + 9
        assert np.isnan(label[last_row]), f"last day of symbol {symbol_idx} must have NaN label"


def test_ridge_also_detects_planted_signal() -> None:
    panel = _synthetic_panel(n_days=120, n_names=80, signal_strength=0.05, seed=5)
    config, scores, labels, groups, symbols, spreads = _scores_for(panel, ModelKind.RIDGE)
    curve = threshold_curve(
        scores,
        labels,
        groups,
        symbols,
        spreads,
        cuts=CUTS,
        capital=1_000_000.0,
        cost_mult=1.0,
        slippage_bps=1.0,
        borrow_bps_annual=50.0,
        periods_per_year=252.0,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    assert curve.auc > 0.6
    assert money.total_pnl > 0
