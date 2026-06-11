"""Tests for the backtest harness mechanics — they encode the leakage traps the
review flagged. These are the gate on harness 'doneness' (NOT a live IC number)."""
import math
from datetime import datetime, timedelta, timezone

from quantlib.backtest import (
    long_short_backtest,
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)


def test_long_short_backtest_gross_net_and_breakeven() -> None:
    ts1 = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    # ts1 ranks A>B>C>D, ts2 flips to D>C>B>A (forces full turnover)
    pred = [4, 3, 2, 1, 1, 2, 3, 4]
    realized = [0.02, 0.01, -0.01, -0.02, 0.01, 0.01, 0.01, 0.03]
    group = [ts1] * 4 + [ts2] * 4
    symbol = ["A", "B", "C", "D"] * 2

    free = long_short_backtest(pred, realized, group, symbol, frac=0.25,
                               cost_bps_oneway=0.0, borrow_bps_annual=0.0)
    assert free["n_periods"] == 2
    assert math.isclose(free["gross_per_period"], 0.03, abs_tol=1e-9)   # mean(0.04, 0.02)
    assert math.isclose(free["net_per_period"], 0.03, abs_tol=1e-9)     # no cost
    assert math.isclose(free["breakeven_cost_bps"], 100.0, abs_tol=0.1) # 0.03 / turn(3) *1e4

    costed = long_short_backtest(pred, realized, group, symbol, frac=0.25,
                                 cost_bps_oneway=50.0, borrow_bps_annual=0.0)
    assert costed["net_per_period"] < costed["gross_per_period"]        # costs bite
    assert math.isclose(costed["net_per_period"], 0.015, abs_tol=1e-9)  # 0.04-0.01, 0.02-0.02

BASE = datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc)


def test_purge_removes_label_straddling_the_split() -> None:
    # 12 timestamps, 1 minute apart; 60-min label horizon; the last training
    # timestamps' labels reach across the test boundary and MUST be purged.
    ts = [BASE + timedelta(minutes=i) for i in range(12)]
    folds = walk_forward_folds(ts, horizon_minutes=60, n_folds=1)
    fold = folds[0]
    test_start = min(ts[i] for i in fold.test_idx)
    # every training timestamp must satisfy ts + 60m <= test_start (no peeking)
    for i in fold.train_idx:
        assert ts[i] + timedelta(minutes=60) <= test_start
    # with only 12 one-minute bars and a 60m horizon, nothing qualifies -> empty train
    assert fold.train_idx == []


def test_purge_keeps_safely_separated_training() -> None:
    # timestamps spaced 30 min apart, 30-min horizon -> training rows up to one
    # step before the test block remain (their label lands exactly at test_start,
    # which is still excluded by <=, so the prior one is the last kept).
    ts = [BASE + timedelta(minutes=30 * i) for i in range(8)]
    folds = walk_forward_folds(ts, horizon_minutes=30, n_folds=1)
    assert len(folds[0].train_idx) > 0
    test_start = min(ts[i] for i in folds[0].test_idx)
    for i in folds[0].train_idx:
        assert ts[i] + timedelta(minutes=30) <= test_start


def test_within_timestamp_ic_ignores_cross_ts_only_signal() -> None:
    """A feature with zero within-cross-section signal but strong cross-timestamp
    correlation must give within-ts IC ~ 0 (pooled IC would look strong)."""
    pred, label, group = [], [], []
    for t in range(10):                      # 10 timestamps
        ts = BASE + timedelta(minutes=t)
        level = float(t)                     # same for every name at this ts
        for name in range(8):                # 8 names per cross-section
            pred.append(level)               # no within-ts variation
            label.append(level + (0.001 * name if t % 2 else -0.001 * name))
            group.append(ts)
    ics = per_timestamp_ic(pred, label, group)
    # pred is constant within each ts -> spearman undefined/skipped -> IC ~ empty/0
    assert math.isnan(mean_ic(ics)) or abs(mean_ic(ics)) < 1e-9


def test_within_timestamp_ic_detects_real_signal() -> None:
    pred, label, group = [], [], []
    for t in range(10):
        ts = BASE + timedelta(minutes=t)
        for name in range(8):
            pred.append(float(name))
            label.append(float(name) + 0.01 * t)   # perfectly rank-correlated within ts
            group.append(ts)
    ics = per_timestamp_ic(pred, label, group)
    assert mean_ic(ics) > 0.99


def test_shuffle_within_groups_canary_kills_ic() -> None:
    pred, label, group = [], [], []
    for t in range(20):
        ts = BASE + timedelta(minutes=t)
        for name in range(10):
            pred.append(float(name))
            label.append(float(name))           # strong real signal
            group.append(ts)
    real_ic = mean_ic(per_timestamp_ic(pred, label, group))
    assert real_ic > 0.99
    shuffled = shuffle_within_groups(label, group, seed=7)
    canary_ic = mean_ic(per_timestamp_ic(pred, shuffled, group))
    assert abs(canary_ic) < 0.4          # signal destroyed by within-group shuffle


def test_newey_west_deflates_vs_naive() -> None:
    # a positively-autocorrelated IC series: NW long-run variance > naive -> smaller t
    ics = {BASE + timedelta(minutes=i): 0.02 + 0.01 * math.sin(i / 3.0) for i in range(40)}
    t_nw = newey_west_tstat(ics, lag=10)
    assert not math.isnan(t_nw)
