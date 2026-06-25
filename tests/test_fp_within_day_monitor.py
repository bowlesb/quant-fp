"""Tests for the WDPC phase-3 monitor loop (quantlib/features/within_day_monitor.py).

Cover the pure pieces offline: the per-cycle clean/dirty decision (evaluate_summary) and the replay window
walk (_replay_windows). The loop itself (claim → compare → streak → certify) is exercised by the live/replay
runs; here we pin the decision logic that drives the streak.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import within_day_monitor
from quantlib.features.within_day_monitor import (_replay_windows,
                                                  evaluate_summary, monitor)


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


def test_no_comparable_cells_holds_streak_not_a_mismatch() -> None:
    # Live cells exist but the settled-window backfill side has no overlapping cell yet (all extra_live →
    # n_compared==0, value_rate=None). This is a COVERAGE GAP, not a divergence: every feature is skipped,
    # results is empty, and the monitor holds the streak (same signal as an empty summary) — it must NOT be
    # graded as a defect that resets stability.
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": None, "n_compared": 0},
            {"feature": "mean_abs_ret_3m", "value_rate": None, "n_compared": 0},
        ]
    )
    clean, results = evaluate_summary(summary, "momentum", dt.date(2026, 6, 18), 1, 20, 20.0)
    assert clean is False
    assert results == []


def test_partial_comparability_grades_only_the_comparable_features() -> None:
    # One feature has settled overlap (graded), the other is a coverage gap (skipped). The graded feature
    # passes → the cycle is clean on the comparable evidence; the non-comparable feature does not defect it.
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": 1.0, "n_compared": 400},
            {"feature": "mean_abs_ret_3m", "value_rate": None, "n_compared": 0},
        ]
    )
    clean, results = evaluate_summary(summary, "momentum", dt.date(2026, 6, 18), 2, 20, 20.0)
    assert clean is True
    assert [r.feature for r in results] == ["up_ratio_3m"]
    assert all(r.status == "certified" for r in results)


def test_monitor_grants_clean_feature_independently_of_divergent_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The structural fix: a parity-clean feature earns its grant on its OWN streak, even though a sibling in
    # the same group irreducibly diverges every cycle. up_ratio_3m always passes (vr=1.0); mean_abs_ret_3m
    # always fails (vr=0.5). With stable_cycles=2, up_ratio_3m must grant (once) by cycle 2; mean_abs_ret_3m
    # must NEVER grant; the run ends at max_cycles having granted exactly the clean feature.
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": 1.0, "n_compared": 400},
            {"feature": "mean_abs_ret_3m", "value_rate": 0.5, "n_compared": 400},
        ]
    )
    granted_calls: list[list[str]] = []

    monkeypatch.setattr(within_day_monitor, "sample_symbols", lambda *a, **k: ["AAA", "BBB"])
    monkeypatch.setattr(within_day_monitor, "compare_window", lambda *a, **k: summary)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "claim", lambda *a, **k: True)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "heartbeat", lambda *a, **k: True)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "release", lambda *a, **k: True)

    def fake_write(results: list, dry_run: bool = True) -> dict[str, int]:
        granted_calls.append([r.feature for r in results])
        return {"granted": len(results)}

    monkeypatch.setattr(within_day_monitor, "write_certifications", fake_write)

    result = monitor(
        "/store",
        "momentum",
        "agent-test",
        mode="replay",
        day=dt.date(2026, 6, 18),
        stable_cycles_required=2,
        max_cycles=4,
        materialize_backfill=False,
    )

    # up_ratio_3m granted exactly once (at its 2nd clean cycle); mean_abs_ret_3m never granted.
    all_granted = [feature for call in granted_calls for feature in call]
    assert all_granted == ["up_ratio_3m"], all_granted
    assert result is not None and result.feature == "up_ratio_3m"


def test_monitor_grants_each_feature_at_its_own_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both features are parity-clean and both must grant (each on its own streak) — the all-clean group still
    # certifies under the per-feature path, granting every comparable feature exactly once.
    summary = _summary(
        [
            {"feature": "up_ratio_3m", "value_rate": 1.0, "n_compared": 400},
            {"feature": "mean_abs_ret_3m", "value_rate": 1.0, "n_compared": 400},
        ]
    )
    granted_calls: list[list[str]] = []
    monkeypatch.setattr(within_day_monitor, "sample_symbols", lambda *a, **k: ["AAA"])
    monkeypatch.setattr(within_day_monitor, "compare_window", lambda *a, **k: summary)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "claim", lambda *a, **k: True)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "heartbeat", lambda *a, **k: True)
    monkeypatch.setattr(within_day_monitor.within_day_assignment, "release", lambda *a, **k: True)
    monkeypatch.setattr(
        within_day_monitor,
        "write_certifications",
        lambda results, dry_run=True: granted_calls.append([r.feature for r in results]) or {},
    )

    result = monitor(
        "/store",
        "momentum",
        "agent-test",
        mode="replay",
        day=dt.date(2026, 6, 18),
        stable_cycles_required=2,
        max_cycles=4,
    )
    all_granted = sorted(feature for call in granted_calls for feature in call)
    assert all_granted == ["mean_abs_ret_3m", "up_ratio_3m"]
    assert result is not None  # certified once both granted


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
