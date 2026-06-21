"""Tests for `StaleEntryTracker` — the bounded genuine-not-found detector that stops the reconcile spin.

The invariant under test (the Lead's care note): an entry is declared TERMINAL only after N CONSECUTIVE
genuine not-founds spanning >= M seconds; a single 404, a transient error that resets the streak, or the
order reappearing must NEVER expire a live/in-flight order.
"""

from __future__ import annotations

import datetime as dt

from strategies.lib.stale_entry import StaleEntryTracker, StalePendingExitTracker

T0 = dt.datetime(2026, 6, 20, 15, 0, tzinfo=dt.timezone.utc)


def test_single_not_found_is_not_terminal() -> None:
    tracker = StaleEntryTracker(min_checks=5, min_seconds=30.0)
    assert tracker.record_not_found("c", T0) is False  # one 404 never expires an order


def test_terminal_only_after_n_checks_and_m_seconds() -> None:
    tracker = StaleEntryTracker(min_checks=5, min_seconds=30.0)
    # 5 consecutive not-founds, but spread so the LAST clears both bounds (>=5 checks AND >=30s).
    terminal = [
        tracker.record_not_found("c", T0 + dt.timedelta(seconds=secs)) for secs in (0, 10, 20, 30, 40)
    ]
    assert terminal == [False, False, False, False, True]  # only the 5th, past 30s, is terminal
    assert tracker.streak_count("c") == 5


def test_enough_checks_but_too_fast_is_not_terminal() -> None:
    """5 not-founds within a few seconds (a fast tick burst) is NOT terminal — the time bound guards a
    transient broker blip that 404s rapidly."""
    tracker = StaleEntryTracker(min_checks=5, min_seconds=30.0)
    results = [tracker.record_not_found("c", T0 + dt.timedelta(seconds=s)) for s in (0, 1, 2, 3, 4)]
    assert not any(results)  # 5 checks but only 4s elapsed -> not yet terminal


def test_reset_breaks_the_streak() -> None:
    """A non-not-found outcome (order appeared / transient error) resets the streak, so only a CONSECUTIVE
    run can ever reach terminal — a live order that momentarily 404s then reappears is never expired."""
    tracker = StaleEntryTracker(min_checks=3, min_seconds=0.0)
    assert tracker.record_not_found("c", T0) is False
    assert tracker.record_not_found("c", T0 + dt.timedelta(seconds=10)) is False
    tracker.reset("c")  # the order reappeared (or a transient error) -> streak cleared
    assert tracker.streak_count("c") == 0
    # the count restarts from 1; it takes a fresh full streak to reach terminal.
    assert tracker.record_not_found("c", T0 + dt.timedelta(seconds=20)) is False


def test_forget_drops_tracking() -> None:
    tracker = StaleEntryTracker(min_checks=2, min_seconds=0.0)
    tracker.record_not_found("c", T0)
    tracker.forget("c")
    assert tracker.streak_count("c") == 0


def test_independent_per_coid() -> None:
    tracker = StaleEntryTracker(min_checks=2, min_seconds=0.0)
    assert tracker.record_not_found("a", T0) is False
    assert tracker.record_not_found("b", T0) is False  # b's streak is independent of a's
    assert tracker.record_not_found("a", T0 + dt.timedelta(seconds=1)) is True  # a reaches terminal
    assert tracker.streak_count("b") == 1  # b unaffected


def test_stale_pending_exit_single_observation_not_stale() -> None:
    tracker = StalePendingExitTracker(min_checks=5, min_seconds=900.0)
    assert tracker.record_pending("x", T0) is False  # one pending tick never declares stale


def test_stale_pending_exit_stale_only_after_checks_and_seconds() -> None:
    """An exit is declared STALE only after N consecutive pending observations spanning >= M seconds, so a
    fast burst of pending ticks during a live fill window never wrongly cancels an in-flight exit."""
    tracker = StalePendingExitTracker(min_checks=5, min_seconds=900.0)
    fast = [tracker.record_pending("x", T0 + dt.timedelta(seconds=s)) for s in (0, 1, 2, 3, 4)]
    assert not any(fast)  # 5 checks but only 4s elapsed -> not stale (a live in-flight exit)
    # spanning a session boundary: the 5th check past 900s declares it stale.
    tracker2 = StalePendingExitTracker(min_checks=5, min_seconds=900.0)
    spanned = [tracker2.record_pending("y", T0 + dt.timedelta(seconds=s)) for s in (0, 300, 600, 900, 1200)]
    assert spanned == [False, False, False, False, True]


def test_stale_pending_exit_reset_breaks_streak() -> None:
    tracker = StalePendingExitTracker(min_checks=2, min_seconds=0.0)
    assert tracker.record_pending("x", T0) is False
    tracker.reset("x")  # the exit filled / vanished -> streak cleared
    assert tracker.streak_count("x") == 0
    assert tracker.record_pending("x", T0 + dt.timedelta(seconds=1)) is False  # restarts from 1
