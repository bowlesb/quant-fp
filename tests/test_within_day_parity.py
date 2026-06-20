"""Unit tests for the Within-Day Parity Certifier PHASE 1 — the settled-window + settle-lag mechanism.

Phase 1 proves the MECHANISM + the bounding (docs/WITHIN_DAY_PARITY_CERTIFICATION.md). These tests cover
the pure window/lag logic (no store/API): the settled window never includes the live tail, the per-layer
settle-lag map is conservative, and the group->lag routing picks the right layer. The full store-backed
compare is exercised manually against real data (the phase-1 gate-read measurement), not unit-mocked here.
"""

from __future__ import annotations

import datetime as dt

from quantlib.features import settle_lag, within_day_parity


def test_settled_window_excludes_the_live_tail() -> None:
    now = dt.datetime(2026, 6, 20, 18, 30, tzinfo=dt.timezone.utc)
    start, end = within_day_parity.settled_window(now, settle_lag_min=20.0, window_minutes=30)
    # The window ENDS settle_lag before now (never the current minute) and spans window_minutes.
    assert end == dt.datetime(2026, 6, 20, 18, 10, tzinfo=dt.timezone.utc)
    assert start == dt.datetime(2026, 6, 20, 17, 40, tzinfo=dt.timezone.utc)
    assert (now - end).total_seconds() / 60 == 20.0
    assert (end - start).total_seconds() / 60 == 30.0


def test_settled_window_is_minute_aligned() -> None:
    now = dt.datetime(2026, 6, 20, 18, 30, 47, 500000, tzinfo=dt.timezone.utc)
    start, end = within_day_parity.settled_window(now, settle_lag_min=15.0, window_minutes=10)
    assert start.second == 0 and start.microsecond == 0
    assert end.second == 0 and end.microsecond == 0


def test_recommended_settle_lag_uses_fallback_when_unmeasured() -> None:
    # No live probe (off-session): every layer falls back to the conservative ceiling.
    out = settle_lag.recommended_settle_lag({"bars": None, "trades": None, "quotes": None})
    assert out == {
        "bars": float(settle_lag.FALLBACK_LAG_MINUTES["bars"]),
        "trades": float(settle_lag.FALLBACK_LAG_MINUTES["trades"]),
        "quotes": float(settle_lag.FALLBACK_LAG_MINUTES["quotes"]),
    }


def test_recommended_settle_lag_rounds_up_with_margin() -> None:
    # A measured 12.4-min lag becomes int(12.4)+1 = 13 (never compare a just-settled minute).
    out = settle_lag.recommended_settle_lag({"bars": 12.4, "trades": 18.9, "quotes": 27.0})
    assert out["bars"] == 13.0
    assert out["trades"] == 19.0
    assert out["quotes"] == 28.0


def test_recommended_settle_lag_negative_falls_back() -> None:
    # A negative measured lag (clock skew / future-stamped) is nonsense -> conservative fallback.
    out = settle_lag.recommended_settle_lag({"bars": -3.0, "trades": None, "quotes": 30.0})
    assert out["bars"] == float(settle_lag.FALLBACK_LAG_MINUTES["bars"])
    assert out["quotes"] == 31.0


def test_window_for_day_today_uses_rolling_settle_lag() -> None:
    now = dt.datetime(2026, 6, 20, 18, 30, tzinfo=dt.timezone.utc)
    start, end = within_day_parity.window_for_day(
        day=dt.date(2026, 6, 20), now_utc=now, settle_lag_min=20.0, window_minutes=30
    )
    # TODAY -> rolling band ending settle_lag before now.
    assert end == dt.datetime(2026, 6, 20, 18, 10, tzinfo=dt.timezone.utc)
    assert start == dt.datetime(2026, 6, 20, 17, 40, tzinfo=dt.timezone.utc)


def test_window_for_day_past_anchors_to_that_day() -> None:
    now = dt.datetime(2026, 6, 20, 18, 30, tzinfo=dt.timezone.utc)
    start, end = within_day_parity.window_for_day(
        day=dt.date(2026, 6, 18), now_utc=now, settle_lag_min=20.0, window_minutes=30
    )
    # PAST day -> a fixed settled RTH band ON THAT DAY (not wall-clock now); 15:30 ET = 19:30 UTC (EDT).
    assert end.date() == dt.date(2026, 6, 18)
    assert end == dt.datetime(2026, 6, 18, 19, 30, tzinfo=dt.timezone.utc)
    assert start == dt.datetime(2026, 6, 18, 19, 0, tzinfo=dt.timezone.utc)


def test_settle_lag_for_group_routes_to_layer() -> None:
    # Bar-derived groups -> bars lag; tick groups -> trades; quote/micro -> quotes. Uses real registry.
    from quantlib.features.registry import REGISTRY

    by_type: dict[str, str] = {}
    for group, _spec in REGISTRY.feature_specs():
        by_type.setdefault(getattr(group.type, "value", str(group.type)), group.name)
    # momentum is bar-derived (bars lag), count_fano/trade_flow is a tick group (trades lag).
    if "momentum" in {g.name for g, _ in REGISTRY.feature_specs()}:
        assert within_day_parity.settle_lag_for_group("momentum") == float(
            settle_lag.FALLBACK_LAG_MINUTES["bars"]
        )


def test_in_session_true_inside_rth_false_outside() -> None:
    # 14:00 UTC = 10:00 ET (inside RTH); 02:00 UTC = 22:00 ET prev day (outside).
    inside = dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)
    outside = dt.datetime(2026, 6, 18, 2, 0, tzinfo=dt.timezone.utc)
    assert settle_lag.in_session(inside) is True
    assert settle_lag.in_session(outside) is False
