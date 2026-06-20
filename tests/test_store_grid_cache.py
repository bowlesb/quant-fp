"""Unit tests for the MongoDB store-grid cache layer (services/dashboard/store_grid_cache).

No real Mongo and no real store/polars: the grid/drill BUILDERS are monkeypatched to tiny payloads and a
module-level FakeMongo stands in for the pymongo client, so the write -> read round-trip, the cold-cache
``booting`` fallback (None), and the unreachable-Mongo fallback are exercised in isolation. This is the cache
contract the /api/store-grid/* routes depend on (an indexed document read off the request path, never a hang
on a cold/unreachable cache, and last-good served between worker builds).
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pymongo.errors import PyMongoError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import store_grid_cache as sgc  # noqa: E402  (path inserted above)


class FakeCollection:
    """In-memory stand-in for a pymongo Collection — only ``replace_one`` / ``find_one`` by ``_id`` (what the
    cache uses). Optionally raises a PyMongoError on every call to exercise the unreachable-Mongo fallback.
    """

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


def _fake_grid() -> dict[str, object]:
    return {
        "generated_at": "2026-06-20T00:00:00Z",
        "anchor_date": "2026-06-18",
        "lookback_days": 5,
        "tickers": ["AAA", "BBB"],
        "summary": {
            "n_dates": 3,
            "n_tickers": 2,
            "n_groups": 4,
            "n_trusted_groups": 1,
            "mean_coverage_pct": 12.5,
        },
    }


def _fake_drill(symbol: str, *_args: object, **_kwargs: object) -> dict[str, object]:
    return {"symbol": symbol, "groups": [], "dates": [], "cells": {}}


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeMongo:
    """Patch the builders + the Mongo client to in-memory fakes; return the shared FakeMongo so a test can
    inspect what was written."""
    fake = FakeMongo()
    monkeypatch.setattr(sgc, "gather_window", lambda *a, **k: object())
    monkeypatch.setattr(sgc, "build_store_grid", lambda *a, **k: _fake_grid())
    monkeypatch.setattr(sgc, "build_ticker_drill", _fake_drill)
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GRID_MONGO_URL: fake)
    return fake


def test_write_then_read_round_trip(patched: FakeMongo) -> None:
    summary = sgc.write_grid(root="/x", lookback_days=5, drill_prewarm=2)
    assert summary["n_tickers"] == 2
    assert summary["drills_prewarmed"] == 2
    assert summary["gzip_bytes"] > 0

    # The grid reads back as the EXACT gzip bytes the writer stored (route serves them with Content-Encoding).
    blob = sgc.read_grid_gzip()
    assert blob is not None
    assert json.loads(gzip.decompress(blob))["tickers"] == ["AAA", "BBB"]

    # Meta reads back without the Mongo _id.
    meta = sgc.read_meta()
    assert meta is not None
    assert "_id" not in meta
    assert meta["n_groups"] == 4

    # A pre-warmed drill is served from the document (uppercased id), not rebuilt.
    drill = sgc.read_drill("aaa")
    assert drill["symbol"] == "AAA"


def test_read_grid_cold_cache_returns_none(patched: FakeMongo) -> None:
    # Nothing written yet -> read returns None so the route reports the one-time booting state.
    assert sgc.read_grid_gzip() is None
    assert sgc.read_meta() is None


def test_read_grid_unreachable_mongo_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GRID_MONGO_URL: FakeMongo(fail=True))
    assert sgc.read_grid_gzip() is None
    assert sgc.read_meta() is None


def test_read_drill_unwarmed_falls_back_to_live_build(monkeypatch: pytest.MonkeyPatch) -> None:
    # Un-warmed ticker (empty cache) -> a live one-ticker build, not an error.
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GRID_MONGO_URL: FakeMongo())
    monkeypatch.setattr(sgc, "build_ticker_drill", _fake_drill)
    drill = sgc.read_drill("zzz")
    assert drill["symbol"] == "ZZZ"
