"""Tests for the CRYPTO within-day canary (quantlib/features/crypto_within_day.py).

Pin the two crypto seams + the streak/cert composition offline (no live websocket, no DB, no /store):

  * the crypto compare window has NO RTH/calendar gate — just a small settle lag (24/7);
  * compare_crypto_window matches live==backfill cell-for-cell over a fed in-memory store, with NO rth_mask
    (a window minute that the equity rth_mask would drop is still compared);
  * the crypto materialize SHIM is dry-run-safe (returns 0, no recompute) and recomputes via the crypto
    backfill path under a live run;
  * plan_crypto_cert reports the cert rows a clean streak would write (reusing the source-agnostic cert plan).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import crypto_within_day as cwd
from quantlib.features.within_day_trust import CertResult

GROUP = "price_returns"  # crypto-applicable (not SPY-relative), real ret_1m feature
DAY = dt.date(2026, 6, 21)


def test_crypto_window_has_no_rth_or_calendar_gate() -> None:
    # 03:07 UTC is the dead of night (no equity session) — crypto still has a valid settled window there.
    now = dt.datetime(2026, 6, 21, 3, 7, tzinfo=dt.timezone.utc)
    start, end = cwd.crypto_settled_window(now, settle_lag_min=2.0, window_minutes=30)
    assert end == dt.datetime(2026, 6, 21, 3, 5, tzinfo=dt.timezone.utc)  # 2-min lag, seconds zeroed
    assert (end - start) == dt.timedelta(minutes=30)


def test_materialize_shim_dry_run_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(cwd, "materialize_crypto_backfill", lambda root, day: called.append(day) or 7)
    count = cwd.materialize_crypto_window(
        "/store/crypto",
        GROUP,
        DAY,
        ["BTC/USD"],
        raw_root="",
        ensure_inputs_first=False,
        agent_id="x",
        dry_run=True,
    )
    assert count == 0
    assert called == []  # dry-run never recomputes


def test_materialize_shim_live_recomputes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cwd, "materialize_crypto_backfill", lambda root, day: 5)
    count = cwd.materialize_crypto_window(
        "/store/crypto",
        GROUP,
        DAY,
        ["BTC/USD"],
        raw_root="",
        ensure_inputs_first=False,
        agent_id="x",
        dry_run=False,
    )
    assert count == 5


def _store_with(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def test_compare_crypto_window_matches_off_rth(monkeypatch: pytest.MonkeyPatch) -> None:
    # A window minute (02:00 UTC) the equity rth_mask would DROP must still be compared for crypto.
    minute = dt.datetime(2026, 6, 21, 2, 0, tzinfo=dt.timezone.utc)
    live = _store_with([{"symbol": "BTC/USD", "minute": minute, "ret_1m": 0.001}])
    backfill = _store_with([{"symbol": "BTC/USD", "minute": minute, "ret_1m": 0.001}])

    def fake_get_features(names, symbols, start, end, root, source, **kw):  # type: ignore[no-untyped-def]
        return live.select(["symbol", "minute", "ret_1m"]) if source == "stream" else backfill

    monkeypatch.setattr(cwd.store, "get_features", fake_get_features)
    window_start = dt.datetime(2026, 6, 21, 1, 45, tzinfo=dt.timezone.utc)
    window_end = dt.datetime(2026, 6, 21, 2, 5, tzinfo=dt.timezone.utc)
    summary = cwd.compare_crypto_window("/store/crypto", GROUP, DAY, ["BTC/USD"], window_start, window_end)

    ret_row = summary.filter(pl.col("feature") == "ret_1m")
    assert ret_row.height == 1
    assert ret_row["n_compared"][0] == 1
    assert ret_row["value_rate"][0] == 1.0  # matched off-RTH


def test_compare_crypto_window_flags_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    minute = dt.datetime(2026, 6, 21, 2, 0, tzinfo=dt.timezone.utc)
    live = _store_with([{"symbol": "BTC/USD", "minute": minute, "ret_1m": 0.001}])
    backfill = _store_with([{"symbol": "BTC/USD", "minute": minute, "ret_1m": 0.999}])  # diverged

    def fake_get_features(names, symbols, start, end, root, source, **kw):  # type: ignore[no-untyped-def]
        return live.select(["symbol", "minute", "ret_1m"]) if source == "stream" else backfill

    monkeypatch.setattr(cwd.store, "get_features", fake_get_features)
    summary = cwd.compare_crypto_window(
        "/store/crypto",
        GROUP,
        DAY,
        ["BTC/USD"],
        dt.datetime(2026, 6, 21, 1, 45, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 21, 2, 5, tzinfo=dt.timezone.utc),
    )
    ret_row = summary.filter(pl.col("feature") == "ret_1m")
    assert ret_row["n_mismatch"][0] == 1
    assert ret_row["value_rate"][0] == 0.0


def test_compare_crypto_window_empty_backfill_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cwd.store, "get_features", lambda *a, **k: pl.DataFrame())
    summary = cwd.compare_crypto_window(
        "/store/crypto",
        GROUP,
        DAY,
        ["BTC/USD"],
        dt.datetime(2026, 6, 21, 1, 45, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 21, 2, 5, tzinfo=dt.timezone.utc),
    )
    assert summary.height == 0


def test_plan_crypto_cert_counts_certified() -> None:
    results = [
        CertResult("ret_1m", "1.0.0", GROUP, DAY.isoformat(), "certified", 1.0, 3, 30, 1, 100, 2.0),
        CertResult("ret_2m", "1.0.0", GROUP, DAY.isoformat(), "defected", 0.4, 0, 30, 1, 100, 2.0),
    ]
    n_cert_rows, n_certified = cwd.plan_crypto_cert(results)
    assert n_cert_rows == 2  # both features get a cert stamp (certified + defected)
    assert n_certified == 1  # only one is 'certified' (would earn a grant)
