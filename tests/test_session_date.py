"""Regression test for the ET trading-session date (task #13).

The bug: maybe_build_universe / maybe_refresh used datetime.now(timezone.utc).date(), the UTC
CALENDAR date. Near midnight UTC (≈ 19:00-20:00 ET) that rolls to the next day, so an evening
build stamps the WRONG session — e.g. a Friday-evening run writes a Saturday trade_date, or a
date a day ahead of the real session. The correct session date is the date in America/New_York.

This pins the contract with a pure reference (et_session_date) and near-midnight-UTC cases. The
production fix (in services/scheduler) must satisfy these same assertions — ideally by importing
this helper instead of re-deriving it, so code and test share one definition.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def et_session_date(now_utc: datetime) -> date:
    """The ET calendar date of a UTC instant — the correct trading-session date."""
    return now_utc.astimezone(NY).date()


def test_friday_evening_utc_midnight_stays_friday_session() -> None:
    # 2026-06-13 00:30 UTC = 2026-06-12 20:30 EDT (Friday evening). The buggy UTC .date()
    # would say 2026-06-13 (Saturday — not a session); ET says Friday 2026-06-12.
    now_utc = datetime(2026, 6, 13, 0, 30, tzinfo=timezone.utc)
    assert now_utc.date() == date(2026, 6, 13)  # the buggy value (documents the trap)
    assert et_session_date(now_utc) == date(2026, 6, 12)  # correct session


def test_late_evening_et_does_not_advance_the_session() -> None:
    # 2026-06-12 23:30 UTC = 19:30 EDT, still Friday in ET.
    assert et_session_date(datetime(2026, 6, 12, 23, 30, tzinfo=timezone.utc)) == date(
        2026, 6, 12
    )


def test_midday_utc_matches_in_both_calendars() -> None:
    # 2026-06-12 14:00 UTC = 10:00 EDT — same date either way (sanity).
    now_utc = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    assert et_session_date(now_utc) == now_utc.date() == date(2026, 6, 12)


def test_est_winter_boundary() -> None:
    # Winter (EST, UTC-5). 2026-01-09 04:30 UTC = 2026-01-08 23:30 EST — still Thursday in ET,
    # while UTC .date() has already rolled to Friday 2026-01-09.
    now_utc = datetime(2026, 1, 9, 4, 30, tzinfo=timezone.utc)
    assert now_utc.date() == date(2026, 1, 9)
    assert et_session_date(now_utc) == date(2026, 1, 8)


def test_weekend_instant_never_yields_a_weekday_session() -> None:
    # A genuine Saturday-afternoon ET instant must report Saturday (caller then skips it);
    # the point is we never silently turn a Friday-evening build into a Saturday session.
    saturday = datetime(2026, 6, 13, 18, 0, tzinfo=timezone.utc)  # 14:00 EDT Sat
    assert et_session_date(saturday).isoweekday() == 6
