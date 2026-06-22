"""Tests for the WDPC phase-3 monitor loop (quantlib/features/within_day_monitor.py).

Cover the pure pieces offline: the per-cycle clean/dirty decision (evaluate_summary) and the replay window
walk (_replay_windows). The loop itself (claim → compare → streak → certify) is exercised by the live/replay
runs; here we pin the decision logic that drives the streak.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.within_day_monitor import (_replay_windows,
                                                  evaluate_summary)


def _summary(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_empty_summary_is_not_clean() -> None:
    clean, results = evaluate_summary(
        pl.DataFrame(), "momentum", dt.date(2026, 6, 18), 1, 20, 20.0
    )
    assert clean is False
    assert results == []


def test_all_features_pass_is_clean() -> None:
    # up_ratio_3m / mean_abs_ret_3m are real momentum features; value_rate 1.0 is at/above any min_pass_rate.
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": 1.0, "n_compared": 400},
            {"feature": "mean_abs_ret_3m", "value_rate": 1.0, "n_compared": 400},
        ]
    )
    clean, results = evaluate_summary(summary, "momentum", dt.date(2026, 6, 18), 3, 20, 20.0)
    assert clean is True
    assert len(results) == 2
    assert all(r.status == "certified" for r in results)
    assert all(r.stable_cycles == 3 for r in results)


def test_one_feature_below_bar_breaks_the_cycle() -> None:
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": 1.0, "n_compared": 400},
            {"feature": "mean_abs_ret_90m", "value_rate": 0.5, "n_compared": 400},
        ]
    )
    clean, results = evaluate_summary(summary, "momentum", dt.date(2026, 6, 18), 1, 20, 20.0)
    assert clean is False
    assert any(r.status != "certified" for r in results)


def test_replay_windows_are_contiguous_and_sized() -> None:
    windows = _replay_windows(dt.date(2026, 6, 18), window_minutes=20, n_windows=5)
    assert len(windows) == 5
    # each window is window_minutes long
    for start, end in windows:
        assert (end - start) == dt.timedelta(minutes=20)
    # contiguous: each window's end == the next window's start (walked forward), last ends 19:30 UTC
    for (s0, e0), (s1, e1) in zip(windows, windows[1:]):
        assert e0 == s1
    assert windows[-1][1] == dt.datetime(2026, 6, 18, 19, 30, tzinfo=dt.timezone.utc)
