"""Unit tests for the pure (no-I/O) logic of the ops health-check: result aggregation, exit codes,
and the ET phase-threshold detection. The I/O checks (store/DB/Prometheus) are exercised live via
`docker exec feature-computer python -m quantlib.ops.healthcheck`, not here.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from quantlib.ops.healthcheck import (
    PER_MINUTE_BANDS,
    CheckResult,
    Status,
    detect_phase,
    exit_code_for,
    last_trading_day,
    render_json,
    render_text,
    resolve_phase,
    summarize,
)

ET = ZoneInfo("America/New_York")


def _result(name: str, status: Status) -> CheckResult:
    return CheckResult(name=name, status=status, detail=f"{name} detail", metric=1.0)


def test_summarize_counts() -> None:
    results = [
        _result("a", Status.PASS),
        _result("b", Status.PASS),
        _result("c", Status.WARN),
        _result("d", Status.FAIL),
    ]
    assert summarize(results) == (2, 1, 1)


def test_exit_code_zero_when_no_fail() -> None:
    results = [_result("a", Status.PASS), _result("b", Status.WARN)]
    assert exit_code_for(results) == 0


def test_exit_code_one_on_any_fail() -> None:
    results = [_result("a", Status.PASS), _result("b", Status.FAIL)]
    assert exit_code_for(results) == 1


def test_warn_alone_is_exit_zero() -> None:
    results = [_result("a", Status.WARN), _result("b", Status.WARN)]
    assert exit_code_for(results) == 0


def test_detect_phase_premarket() -> None:
    # Monday 08:00 ET -> premarket.
    moment = datetime(2026, 6, 15, 8, 0, tzinfo=ET)
    phase = detect_phase(moment)
    assert phase.name == "premarket"
    assert phase.et_minute_of_day == 480


def test_detect_phase_rth_midsession() -> None:
    moment = datetime(2026, 6, 15, 12, 30, tzinfo=ET)
    phase = detect_phase(moment)
    assert phase.name == "rth"
    assert phase.et_minute_of_day == 750


def test_detect_phase_afterhours() -> None:
    moment = datetime(2026, 6, 15, 17, 0, tzinfo=ET)
    assert detect_phase(moment).name == "afterhours"


def test_detect_phase_closed_overnight() -> None:
    moment = datetime(2026, 6, 15, 2, 0, tzinfo=ET)
    assert detect_phase(moment).name == "closed"


def test_detect_phase_closed_weekend() -> None:
    # 2026-06-13 is a Saturday — always closed even mid-day.
    moment = datetime(2026, 6, 13, 12, 0, tzinfo=ET)
    assert detect_phase(moment).name == "closed"


def test_resolve_phase_override_keeps_requested_name() -> None:
    moment = datetime(2026, 6, 15, 12, 0, tzinfo=ET)
    phase = resolve_phase("premarket", moment)
    assert phase.name == "premarket"
    assert phase.et_minute_of_day == 720


def test_every_phase_has_a_per_minute_band() -> None:
    for phase_name in ("premarket", "rth", "afterhours", "closed"):
        band_lo, band_hi = PER_MINUTE_BANDS[phase_name]
        assert band_lo <= band_hi


def test_last_trading_day_skips_weekend() -> None:
    # Monday 2026-06-15 -> previous trading day is Friday 2026-06-12.
    assert last_trading_day(date(2026, 6, 15)) == date(2026, 6, 12)


def test_render_text_has_summary_line() -> None:
    phase = detect_phase(datetime(2026, 6, 15, 12, 0, tzinfo=ET))
    results = [_result("a", Status.PASS), _result("b", Status.WARN)]
    text = render_text(results, phase)
    assert "HEALTHCHECK 1 PASS / 1 WARN / 0 FAIL" in text


def test_render_json_roundtrip_fields() -> None:
    import json

    phase = detect_phase(datetime(2026, 6, 15, 12, 0, tzinfo=ET))
    results = [_result("a", Status.PASS), _result("b", Status.FAIL)]
    payload = json.loads(render_json(results, phase))
    assert payload["summary"] == {"pass": 1, "warn": 0, "fail": 1}
    assert payload["exit_code"] == 1
    assert {check["name"] for check in payload["checks"]} == {"a", "b"}
