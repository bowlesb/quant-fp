"""Unit tests for the fleet under-rep worker cache + route (services/dashboard/underrep_cache + app).

No real Mongo and no real store/polars: ``build_report`` is monkeypatched to a tiny payload and a module-level
FakeMongo stands in for the pymongo client, so the write -> read round-trip, the cold-cache booting state, the
unreachable-Mongo fallback, and the route shaping are all pinned without infrastructure.

  * write_report builds the report, gzips it into Mongo, and writes a small meta doc; read_report decompresses
    it back; read_meta returns the summary with the Mongo _id stripped;
  * a cold cache (no document) reads None; an unreachable Mongo reads None (the route maps both to booting);
  * the route returns 200 with the cached report, 503 booting on a cold/unreachable cache.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pymongo.errors import PyMongoError

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import underrep_cache as uc  # noqa: E402  (paths inserted above)


class FakeCollection:
    """In-memory pymongo Collection stand-in — replace_one / find_one by ``_id`` (what the cache uses).
    Optionally raises a PyMongoError on every call to exercise the unreachable-Mongo fallback."""

    def __init__(self, fail: bool = False) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}
        self.fail = fail

    def replace_one(self, filt: dict[str, Any], doc: dict[str, Any], upsert: bool = False) -> None:
        if self.fail:
            raise PyMongoError("simulated unreachable mongo")
        self.docs[filt["_id"]] = dict(doc)

    def find_one(self, filt: dict[str, Any]) -> dict[str, Any] | None:
        if self.fail:
            raise PyMongoError("simulated unreachable mongo")
        doc = self.docs.get(filt["_id"])
        return dict(doc) if doc is not None else None


class FakeDatabase:
    def __init__(self, fail: bool = False) -> None:
        self._collections: dict[str, FakeCollection] = {}
        self.fail = fail

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self._collections:
            self._collections[name] = FakeCollection(fail=self.fail)
        return self._collections[name]


class FakeMongo:
    """Minimal pymongo MongoClient stand-in: ``client[db_name]`` -> a FakeDatabase of FakeCollections."""

    def __init__(self, fail: bool = False) -> None:
        self._db = FakeDatabase(fail=fail)

    def __getitem__(self, _name: str) -> FakeDatabase:
        return self._db


def _fake_report() -> dict[str, object]:
    return {
        "n_symbols_seen": 5,
        "n_symbols_backfilled": 4,
        "n_symbols_streamed": 3,
        "n_symbols_under_represented": 2,
        "backfill_dates_sampled": 3,
        "stream_window_days": 7,
        "per_group_gap": {"alpha": 2, "beta": 1},
        "under_represented": [
            {"symbol": "AAA", "under_rep_score": 2, "under_rep_groups": ["alpha", "beta"]},
            {"symbol": "BBB", "under_rep_score": 1, "under_rep_groups": ["alpha"]},
        ],
    }


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeMongo:
    """Patch the report builder + the Mongo client to in-memory fakes; return the shared FakeMongo."""
    fake = FakeMongo()
    monkeypatch.setattr(uc, "build_report", lambda *a, **k: _fake_report())
    monkeypatch.setattr(uc, "PartitionStoreReader", lambda root: object())
    monkeypatch.setattr(uc, "_client", lambda url=uc.UNDERREP_MONGO_URL: fake)
    return fake


def test_write_then_read_round_trip(patched: FakeMongo) -> None:
    summary = uc.write_report()
    assert summary["n_symbols_under_represented"] == 2
    assert "generated_at" in summary
    assert summary["gzip_bytes"] > 0

    report = uc.read_report()
    assert report is not None
    assert report["n_symbols_under_represented"] == 2
    assert report["under_represented"][0]["symbol"] == "AAA"  # type: ignore[index]
    # The report carries the generated_at the worker stamped.
    assert "generated_at" in report


def test_write_stores_gzip_blob(patched: FakeMongo) -> None:
    uc.write_report()
    # The stored doc is a gzip blob that decompresses to the report (proves the read path's contract).
    doc = patched[uc.UNDERREP_DB_NAME][uc.REPORT_COLLECTION].find_one({"_id": uc.REPORT_DOC_ID})
    assert doc is not None
    decoded = json.loads(gzip.decompress(bytes(doc["gzip"])).decode("utf-8"))
    assert decoded["n_symbols_seen"] == 5


def test_read_meta_strips_id(patched: FakeMongo) -> None:
    uc.write_report()
    meta = uc.read_meta()
    assert meta is not None
    assert "_id" not in meta
    assert meta["n_symbols_under_represented"] == 2
    assert meta["build_seconds"] >= 0


def test_read_report_cold_cache_returns_none(patched: FakeMongo) -> None:
    # No write yet -> no document -> None (the route reports booting).
    assert uc.read_report() is None
    assert uc.read_meta() is None


def test_read_report_unreachable_mongo_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "_client", lambda url=uc.UNDERREP_MONGO_URL: FakeMongo(fail=True))
    assert uc.read_report() is None
    assert uc.read_meta() is None


def test_route_returns_200_with_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402  (dashboard dir on path)
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard_app, "read_underrep_report", lambda: _fake_report())
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/underrepresented-tickers")
    assert resp.status_code == 200
    assert resp.json()["n_symbols_under_represented"] == 2
    assert resp.headers["cache-control"] == "no-store"


def test_route_503_when_report_not_built(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard_app, "read_underrep_report", lambda: None)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/underrepresented-tickers")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True


def test_meta_route_200_and_503(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as dashboard_app  # noqa: E402
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard_app, "read_underrep_meta", lambda: {"n_symbols_under_represented": 2})
    client = TestClient(dashboard_app.app)
    assert client.get("/api/underrepresented-tickers/meta").status_code == 200

    monkeypatch.setattr(dashboard_app, "read_underrep_meta", lambda: None)
    resp = client.get("/api/underrepresented-tickers/meta")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True
