"""Tests for the single-entrypoint battery runner + the per-minute look-ahead labels.

The look-ahead label tests use a hand-built synthetic Panel with a KNOWN forward path so the
triple-barrier / run-up math is checked against the exact expected value (not a smoke test).
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.battery.battery_config import (
    BatteryConfig,
    Cadence,
    DataSpec,
    LabelKind,
    SignalKind,
    StrategyConfig,
)
from quantlib.battery.battery_run import build_label, evaluate_strategy, run_battery
from quantlib.battery.lookahead import fwd_max_runup_label, up_move_start_label
from quantlib.battery.panel import Panel

_EPOCH = int(dt.datetime(2026, 1, 2, 14, 0, tzinfo=dt.timezone.utc).timestamp() * 1e9)
_STEP = 60_000_000_000  # 1 minute in ns


def _panel_from_paths(paths: dict[str, list[tuple[float, float, float]]]) -> Panel:
    """Build a minimal Panel from per-symbol (entry_close, high, low) bar tuples in time order.

    The panel is sorted by (symbol_code, minute) as the contract requires. A single shared minute grid
    per symbol (consecutive minutes) — enough to exercise the per-block forward-window labels.
    """
    symbols = sorted(paths)
    sym_to_idx = {s: i for i, s in enumerate(symbols)}
    symbol_code: list[int] = []
    minute_epoch: list[int] = []
    entry: list[float] = []
    high: list[float] = []
    low: list[float] = []
    for sym in symbols:
        for step, (close, hi, lo) in enumerate(paths[sym]):
            symbol_code.append(sym_to_idx[sym])
            minute_epoch.append(_EPOCH + step * _STEP)
            entry.append(close)
            high.append(hi)
            low.append(lo)
    n = len(entry)
    return Panel(
        symbol_code=np.array(symbol_code, dtype=np.int64),
        symbol_names=symbols,
        minute_epoch=np.array(minute_epoch, dtype=np.int64),
        feature_names=["f0"],
        feature_matrix=np.zeros((n, 1)),
        entry_close=np.array(entry, dtype=float),
        half_spread_bps=np.full(n, 3.0),
        high=np.array(high, dtype=float),
        low=np.array(low, dtype=float),
        volume=np.full(n, 1e6),
        extra={},
        cadence="intraday",
    )


def test_up_move_start_hits_up_barrier() -> None:
    # entry 100; bar +1 high 100.6 (>= +50bps=100.5) before any -50bps low -> label +1 at row 0.
    panel = _panel_from_paths({"AAA": [(100.0, 100.0, 100.0), (100.6, 100.6, 100.1), (101.0, 101.0, 100.5)]})
    label = up_move_start_label(panel, horizon_bars=2, barrier_bps=50.0)
    assert label[0] == 1.0
    # the last 2 rows have an incomplete forward window -> NaN
    assert np.isnan(label[1]) and np.isnan(label[2])


def test_up_move_start_hits_down_barrier_first() -> None:
    # entry 100; bar +1 low 99.4 (<= -50bps=99.5) before the up barrier -> label -1.
    panel = _panel_from_paths({"AAA": [(100.0, 100.0, 100.0), (100.2, 100.2, 99.4), (100.0, 100.6, 99.9)]})
    label = up_move_start_label(panel, horizon_bars=2, barrier_bps=50.0)
    assert label[0] == -1.0


def test_up_move_start_timeout_is_zero() -> None:
    # neither barrier touched within H -> 0.
    panel = _panel_from_paths({"AAA": [(100.0, 100.0, 100.0), (100.1, 100.2, 99.9), (100.0, 100.2, 99.8)]})
    label = up_move_start_label(panel, horizon_bars=2, barrier_bps=50.0)
    assert label[0] == 0.0


def test_fwd_max_runup_exact() -> None:
    # entry 100; forward highs over next 2 bars = 102, 101 -> max runup = 102/100 - 1 = 0.02.
    panel = _panel_from_paths({"AAA": [(100.0, 100.0, 100.0), (101.5, 102.0, 101.0), (100.5, 101.0, 100.0)]})
    label = fwd_max_runup_label(panel, horizon_bars=2)
    assert abs(label[0] - 0.02) < 1e-12


def test_lookahead_does_not_cross_symbol_blocks() -> None:
    # two symbols; the forward window of AAA's tail must NOT read BBB's rows.
    panel = _panel_from_paths(
        {
            "AAA": [(100.0, 100.0, 100.0), (100.6, 100.6, 100.1)],
            "BBB": [(50.0, 50.0, 50.0), (50.6, 50.6, 50.1)],
        }
    )
    label = up_move_start_label(panel, horizon_bars=1, barrier_bps=50.0)
    # AAA row 0 gradable (next bar high 100.6 >= 100.5 -> +1); AAA row 1 = block tail -> NaN.
    assert label[0] == 1.0
    assert np.isnan(label[1])
    # BBB row 0 gradable independently; row 1 tail -> NaN.
    assert label[2] == 1.0
    assert np.isnan(label[3])


def test_run_battery_shares_panel_and_times() -> None:
    """A real (cached) battery run: panel loads once, every strategy evaluates, timing is reported."""
    config = BatteryConfig(
        data=DataSpec(
            cadence=Cadence.DAILY,
            date_start="2026-01-01",
            date_end="2026-06-17",
            universe_top=200,
            daily_cache="experiments/data/battery_daily_cache.parquet",
        ),
        strategies=[
            StrategyConfig(
                name="mom_ret_5d",
                signal=SignalKind.FEATURE,
                signal_feature="ret_5d",
                features=("ret_5d",),
                label=LabelKind.FORWARD_EXCESS,
                horizon=1,
            ),
            StrategyConfig(
                name="rev_ret_5d",
                signal=SignalKind.FEATURE,
                signal_feature="ret_5d",
                signal_sign=-1.0,
                features=("ret_5d",),
                label=LabelKind.FORWARD_EXCESS,
                horizon=1,
            ),
        ],
    )
    report = run_battery(config)
    assert len(report.results) == 2
    assert report.n_rows > 0
    assert report.panel_load_seconds >= 0.0
    assert report.total_seconds >= report.panel_load_seconds
    # momentum and its sign-flipped reversal must produce opposite-signed IC (sanity the sign wiring).
    by_name = {r.name: r for r in report.results}
    mom_ic = by_name["mom_ret_5d"].mean_ic
    rev_ic = by_name["rev_ret_5d"].mean_ic
    if mom_ic == mom_ic and rev_ic == rev_ic:
        assert np.sign(mom_ic) != np.sign(rev_ic) or abs(mom_ic) < 1e-9
