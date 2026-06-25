"""Unit tests for the per-ticker coverage dashboard route (services/dashboard/ticker_coverage_route + app).

The heavy lifting (the store walk + report shape) is ticker_coverage's, tested in test_ticker_coverage. Here
we pin the thin route layer with NO store and NO DB:

  * the snapshot wrapper builds the report via ticker_coverage and caches per (symbol, with_trust);
  * it only reads trust when with_trust is set (the lazy engine/DB path stays untouched otherwise);
  * the route returns 200 with the snapshot, 503 booting if the store is not mounted (FileNotFoundError) or
    the trust DB is unreachable (OperationalError) — mirroring the other read routes, never a 500.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import ticker_coverage_route as route  # noqa: E402  (paths inserted above)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    route._cache.clear()


def test_snapshot_builds_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"build": 0, "trust": 0}

    def fake_build(reader: object, symbol: str, trust: object) -> dict[str, object]:
        calls["build"] += 1
        return {"symbol": symbol, "n_groups_covered": 3, "trust_passed": trust}

    def fake_trust() -> dict[str, object]:
        calls["trust"] += 1
        return {"f1": object()}

    monkeypatch.setattr(route, "build_report", fake_build)
    monkeypatch.setattr(route, "read_trust_by_feature", fake_trust)
    monkeypatch.setattr(route, "_store_reader", lambda: object())

    # First call builds; symbol is upper-cased; no trust requested -> trust reader untouched.
    snap = route.ticker_coverage_snapshot("aapl")
    assert snap["symbol"] == "AAPL"
    assert snap["trust_passed"] is None
    assert calls == {"build": 1, "trust": 0}

    # Second identical call is served from cache (no rebuild).
    route.ticker_coverage_snapshot("AAPL")
    assert calls["build"] == 1


def test_with_trust_reads_trust_and_caches_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"build": 0, "trust": 0}

    def fake_build(reader: object, symbol: str, trust: object) -> dict[str, object]:
        calls["build"] += 1
        return {"symbol": symbol, "has_trust": trust is not None}

    def fake_trust() -> dict[str, object]:
        calls["trust"] += 1
        return {"f1": object()}

    monkeypatch.setattr(route, "build_report", fake_build)
    monkeypatch.setattr(route, "read_trust_by_feature", fake_trust)
    monkeypatch.setattr(route, "_store_reader", lambda: object())

    snap = route.ticker_coverage_snapshot("AAPL", with_trust=True)
    assert snap["has_trust"] is True
    assert calls == {"build": 1, "trust": 1}

    # The no-trust variant is a SEPARATE cache key -> a distinct build, no trust read.
    snap2 = route.ticker_coverage_snapshot("AAPL", with_trust=False)
    assert snap2["has_trust"] is False
    assert calls == {"build": 2, "trust": 1}


def test_route_returns_200_with_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402  (dashboard dir on path)
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        dashboard_app, "ticker_coverage_snapshot", lambda symbol, with_trust=False: {"symbol": symbol}
    )
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/ticker-coverage/AAPL")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "AAPL"
    assert resp.headers["cache-control"] == "no-store"


def test_route_503_when_store_not_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402
    from fastapi.testclient import TestClient

    def _raise(symbol: str, with_trust: bool = False) -> dict[str, object]:
        raise FileNotFoundError("/store missing")

    monkeypatch.setattr(dashboard_app, "ticker_coverage_snapshot", _raise)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/ticker-coverage/AAPL")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True


def test_route_503_when_trust_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402
    from fastapi.testclient import TestClient

    def _raise(symbol: str, with_trust: bool = False) -> dict[str, object]:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(dashboard_app, "ticker_coverage_snapshot", _raise)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/ticker-coverage/AAPL?with_trust=1")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True
