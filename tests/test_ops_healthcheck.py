"""Unit tests for the pure (no-I/O) logic of the ops health-check: result aggregation, exit codes,
and the ET phase-threshold detection. The I/O checks (store/DB/Prometheus) are exercised live via
`docker exec feature-computer python -m quantlib.ops.healthcheck`, not here.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from quantlib.ops.healthcheck import (
    PER_MINUTE_BANDS,
    STREAM_COVERAGE_CHECKS,
    CheckResult,
    Status,
    build_registry,
    count_skips,
    detect_phase,
    equity_stream_expected,
    exit_code_for,
    is_market_holiday,
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
    assert payload["summary"] == {"pass": 1, "warn": 0, "fail": 1, "skip": 0}
    assert payload["exit_code"] == 1
    assert {check["name"] for check in payload["checks"]} == {"a", "b"}


def test_is_market_holiday_juneteenth() -> None:
    # 2026-06-19 is Juneteenth — a full-day NYSE closure even though it is a Friday.
    moment = datetime(2026, 6, 19, 12, 0, tzinfo=ET)
    assert is_market_holiday(moment) is True


def test_is_market_holiday_regular_trading_day() -> None:
    # 2026-06-18 (Thursday) is a normal session.
    moment = datetime(2026, 6, 18, 12, 0, tzinfo=ET)
    assert is_market_holiday(moment) is False


def test_equity_stream_expected_trading_minute() -> None:
    # Thursday 12:00 ET RTH — stream expected, checks should evaluate.
    moment = datetime(2026, 6, 18, 12, 0, tzinfo=ET)
    phase = detect_phase(moment)
    assert phase.name == "rth"
    assert equity_stream_expected(phase, moment) is True


def test_equity_stream_not_expected_weekend() -> None:
    # Sunday 12:00 ET — detect_phase => closed, no equity stream.
    moment = datetime(2026, 6, 21, 12, 0, tzinfo=ET)
    phase = detect_phase(moment)
    assert phase.name == "closed"
    assert equity_stream_expected(phase, moment) is False


def test_equity_stream_not_expected_weekday_holiday() -> None:
    # Juneteenth (Fri) 12:00 ET — phase reads rth by clock, but it is a holiday: no stream expected.
    moment = datetime(2026, 6, 19, 12, 0, tzinfo=ET)
    phase = detect_phase(moment)
    assert phase.name == "rth"
    assert equity_stream_expected(phase, moment) is False


def test_registry_skips_stream_checks_off_session() -> None:
    # Sunday: the 5 stream-coverage checks must be replaced by SKIP results. Invoke only those fns (the
    # non-stream checks do real DB/Prometheus I/O — not exercised in this pure-logic test).
    moment = datetime(2026, 6, 21, 12, 0, tzinfo=ET)
    phase = detect_phase(moment)
    registry = dict(build_registry(phase, moment))
    skipped = {name for name in STREAM_COVERAGE_CHECKS if registry[name]().status == Status.SKIP}
    assert skipped == set(STREAM_COVERAGE_CHECKS)


def test_registry_skip_result_is_non_failing() -> None:
    # An off-session run with all stream checks skipped must exit 0 (no FAIL).
    moment = datetime(2026, 6, 21, 12, 0, tzinfo=ET)
    phase = detect_phase(moment)
    registry = build_registry(phase, moment)
    skip_results = [check_fn() for name, check_fn in registry if name in STREAM_COVERAGE_CHECKS]
    assert all(result.status == Status.SKIP for result in skip_results)
    assert exit_code_for(skip_results) == 0
    assert count_skips(skip_results) == len(STREAM_COVERAGE_CHECKS)


def test_registry_keeps_stream_checks_in_session() -> None:
    # On a real trading minute the registry must NOT substitute SKIPs — the original check fns stay.
    in_moment = datetime(2026, 6, 18, 12, 0, tzinfo=ET)
    off_moment = datetime(2026, 6, 21, 12, 0, tzinfo=ET)
    in_session = dict(build_registry(detect_phase(in_moment), in_moment))
    off_session = dict(build_registry(detect_phase(off_moment), off_moment))
    for name in STREAM_COVERAGE_CHECKS:
        assert in_session[name] is not off_session[name]


def test_skip_excluded_from_summarize_counts() -> None:
    results = [_result("a", Status.PASS), _result("b", Status.SKIP), _result("c", Status.SKIP)]
    assert summarize(results) == (1, 0, 0)
    assert count_skips(results) == 2


def test_render_text_shows_skip_suffix() -> None:
    phase = detect_phase(datetime(2026, 6, 21, 12, 0, tzinfo=ET))
    results = [_result("a", Status.PASS), _result("b", Status.SKIP)]
    text = render_text(results, phase)
    assert "1 PASS / 0 WARN / 0 FAIL / 1 SKIP" in text
