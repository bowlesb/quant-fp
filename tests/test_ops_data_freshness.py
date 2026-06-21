"""Unit tests for the pure (no-I/O) logic of the data-freshness alert: business-hours gating, the
market-hours-aware age grading, status-line shape, and the main() exit code. The DB / store reads are
exercised live via `docker exec ... python -m quantlib.ops.data_freshness`, not here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quantlib.ops import data_freshness as df
from quantlib.ops.data_freshness import FreshnessResult, Status, grade_age, in_business_hours

ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


def test_business_hours_weekday_midday_true() -> None:
    # Wednesday 2026-06-17 11:00 ET
    assert in_business_hours(_et(2026, 6, 17, 11, 0)) is True


def test_business_hours_weekday_overnight_false() -> None:
    # Wednesday 2026-06-17 03:00 ET — before 06:00
    assert in_business_hours(_et(2026, 6, 17, 3, 0)) is False


def test_business_hours_weekend_false() -> None:
    # Saturday 2026-06-20 11:00 ET
    assert in_business_hours(_et(2026, 6, 20, 11, 0)) is False


def test_grade_stale_during_business_hours() -> None:
    reference = _et(2026, 6, 17, 14, 0)
    newest = reference - timedelta(minutes=180)
    result = grade_age("edgar", newest, reference, warn_min=45, fail_min=120)
    assert result.status == Status.STALE
    assert result.age_minutes is not None and result.age_minutes >= 120


def test_grade_warn_during_business_hours() -> None:
    reference = _et(2026, 6, 17, 14, 0)
    newest = reference - timedelta(minutes=60)
    result = grade_age("edgar", newest, reference, warn_min=45, fail_min=120)
    assert result.status == Status.WARN


def test_grade_ok_when_fresh() -> None:
    reference = _et(2026, 6, 17, 14, 0)
    newest = reference - timedelta(minutes=5)
    result = grade_age("edgar", newest, reference, warn_min=45, fail_min=120)
    assert result.status == Status.OK


def test_grade_inactive_outside_business_hours_even_when_old() -> None:
    # Saturday: a 2-day-old newest is the expected weekend lull, NOT a stall.
    reference = _et(2026, 6, 20, 11, 0)
    newest = reference - timedelta(days=2)
    result = grade_age("edgar", newest, reference, warn_min=45, fail_min=120)
    assert result.status == Status.INACTIVE


def test_grade_none_in_business_hours_is_error() -> None:
    reference = _et(2026, 6, 17, 14, 0)
    result = grade_age("news", None, reference, warn_min=90, fail_min=240)
    assert result.status == Status.ERROR


def test_grade_none_off_hours_is_inactive() -> None:
    reference = _et(2026, 6, 20, 11, 0)  # weekend
    result = grade_age("news", None, reference, warn_min=90, fail_min=240)
    assert result.status == Status.INACTIVE


def test_status_line_is_json_with_both_sources() -> None:
    reference = _et(2026, 6, 17, 14, 0)
    results = [
        FreshnessResult("edgar", Status.OK, "fresh", reference.isoformat(), 1.0),
        FreshnessResult("news", Status.WARN, "lagging", reference.isoformat(), 95.0),
    ]
    line = df.status_line(results, reference)
    parsed = json.loads(line)
    assert parsed["edgar"]["status"] == "OK"
    assert parsed["news"]["status"] == "WARN"
    assert "ts" in parsed


def test_main_exit_zero_when_no_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        df,
        "run_checks",
        lambda ref=None: [
            FreshnessResult("edgar", Status.OK, "fresh"),
            FreshnessResult("news", Status.WARN, "lagging"),
        ],
    )
    monkeypatch.setattr(df, "now_utc", lambda reference=None: reference or _et(2026, 6, 17, 14, 0))
    assert df.main([]) == 0


def test_main_exit_one_when_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        df,
        "run_checks",
        lambda ref=None: [
            FreshnessResult("edgar", Status.STALE, "stalled"),
            FreshnessResult("news", Status.OK, "fresh"),
        ],
    )
    monkeypatch.setattr(df, "now_utc", lambda reference=None: _et(2026, 6, 17, 14, 0))
    assert df.main([]) == 1
