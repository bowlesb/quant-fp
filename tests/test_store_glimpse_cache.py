"""Unit tests for the persistent (Redis) store-glimpse cache layer (services/dashboard/store_glimpse_cache).

No real Redis and no real store/polars: the grid/drill BUILDERS are monkeypatched to tiny payloads and a
module-level FakeRedis stands in for the bus client, so the write -> read round-trip, the cold-cache
``warming`` fallback, and the unreachable-Redis fallback are exercised in isolation. This is the cache
contract the ``/api/store-glimpse`` routes depend on (instant cache read off the request path, never a hang
on a cold/unreachable cache).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import store_glimpse_cache as sgc  # noqa: E402  (path inserted above)


class FakeRedis:
    """Minimal in-memory stand-in for ``redis.Redis`` — only the ``get``/``set`` the cache uses. ``ex`` (TTL)
    is accepted and ignored (TTL behavior is Redis's, not ours to re-test). Optionally raises a RedisError on
    every call to exercise the unreachable-Redis fallback."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise redis.RedisError("simulated unreachable redis")
        self.store[key] = value

    def get(self, key: str) -> str | None:
        if self.fail:
            raise redis.RedisError("simulated unreachable redis")
        return self.store.get(key)


_FAKE_GRID = {
    "anchor_date": "2026-06-18",
    "summary": {"n_groups": 2, "n_features": 3, "n_dates": 30},
    "groups": [{"group": "groupX"}, {"group": "groupY"}],
    "dates": ["2026-06-18"],
    "cells": {},
}


def _fake_build_grid(root: str, days: int = 30) -> dict[str, object]:
    return dict(_FAKE_GRID)


def _fake_build_drill(group: str, root: str, days: int = 30, limit: int = 500) -> dict[str, object]:
    return {"group": group, "n_tickers": 7, "tickers": [], "dates": []}


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Patch the builders to tiny payloads + the client factory to a shared FakeRedis the test can inspect."""
    fake = FakeRedis()
    monkeypatch.setattr(sgc, "build_store_glimpse", _fake_build_grid)
    monkeypatch.setattr(sgc, "build_ticker_drill", _fake_build_drill)
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GLIMPSE_REDIS_URL: fake)
    return fake


def test_write_then_read_grid_round_trips(patched: FakeRedis) -> None:
    summary = sgc.write_glimpse(root="/store", days=30)
    assert summary["n_groups"] == 2
    assert summary["n_drills"] == 2  # one per group
    assert summary["anchor_date"] == "2026-06-18"

    grid = sgc.read_glimpse(days=30)
    assert grid["anchor_date"] == "2026-06-18"
    assert grid.get("warming", False) is False
    assert grid["summary"]["n_groups"] == 2  # type: ignore[index]


def test_write_then_read_each_group_drill(patched: FakeRedis) -> None:
    sgc.write_glimpse(root="/store", days=30)
    for group in ("groupX", "groupY"):
        drill = sgc.read_drill(group, days=30, limit=500)
        assert drill["group"] == group
        assert drill["n_tickers"] == 7
        assert drill.get("warming", False) is False


def test_read_grid_cold_cache_returns_warming(patched: FakeRedis) -> None:
    # Nothing written yet -> the key is absent -> a valid warming payload, NOT a live build / hang.
    grid = sgc.read_glimpse(days=30)
    assert grid["warming"] is True
    assert grid["dates"] == []
    assert grid["summary"]["n_groups"] == 0  # type: ignore[index]


def test_read_drill_cold_cache_returns_warming(patched: FakeRedis) -> None:
    drill = sgc.read_drill("groupX", days=30, limit=500)
    assert drill["warming"] is True
    assert drill["n_tickers"] == 0
    assert drill["tickers"] == []


def test_read_grid_unreachable_redis_falls_back_to_warming(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeRedis(fail=True)
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GLIMPSE_REDIS_URL: failing)
    grid = sgc.read_glimpse(days=30)
    assert grid["warming"] is True  # a RedisError is swallowed into the warming placeholder, never raised


def test_read_drill_unreachable_redis_falls_back_to_warming(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeRedis(fail=True)
    monkeypatch.setattr(sgc, "_client", lambda url=sgc.GLIMPSE_REDIS_URL: failing)
    drill = sgc.read_drill("groupX", days=30, limit=500)
    assert drill["warming"] is True


def test_keys_are_namespaced_per_window_and_group(patched: FakeRedis) -> None:
    sgc.write_glimpse(root="/store", days=30)
    # The grid key carries the window; each drill key carries group + window + limit. Distinct windows /
    # groups must not collide (a 14d view must never read a 30d blob).
    assert sgc.GRID_KEY.format(days=30) in patched.store
    assert sgc.GRID_KEY.format(days=14) not in patched.store
    assert sgc.DRILL_KEY.format(group="groupX", days=30, limit=500) in patched.store
    assert sgc.DRILL_KEY.format(group="groupX", days=30, limit=500) != sgc.DRILL_KEY.format(
        group="groupY", days=30, limit=500
    )
