"""Tests for the dashboard latency-expectations module + route (services/dashboard).

Covers the read-side accessor ``load_latency_expectations`` (present file -> parsed dict; absent file -> None,
the booting signal) and the ``/api/latency-expectations`` FastAPI route (200 passthrough of the parsed JSON;
503 ``booting`` when the artifact is absent). No real store/Mongo: the JSON path is monkeypatched to a tmp file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import latency_expectations as le  # noqa: E402  (path inserted above)

_SAMPLE = {
    "schema_version": 1,
    "generated_at": "2026-06-21T02:40:00Z",
    "units": "milliseconds",
    "sorted_by": "p99_ms descending (slowest-first)",
    "e2e_context": {
        "metric": "per-bet bar->vector",
        "single_bet_isolated_p50_ms": 289,
        "typical_bet_under_load_p50_ms": 935,
        "target_p99_ms": 100,
        "note": "the e2e gate, not the per-group sum",
    },
    "group_count": 2,
    "feature_count": 90,
    "groups": [
        {
            "group": "price_volume",
            "feat_count": 70,
            "kind": "B incremental-sum",
            "mechanism": "shared running-sum",
            "incremental_ready": "parked",
            "p50_ms": 133.78,
            "p99_ms": 202.25,
        },
        {
            "group": "round_levels",
            "feat_count": 3,
            "kind": "A cached/static",
            "mechanism": "consolidated pass",
            "incremental_ready": "n-a",
            "p50_ms": 0.88,
            "p99_ms": 1.01,
        },
    ],
}


def _write_sample(tmp_path: Path) -> Path:
    path = tmp_path / "feature_latency_expectations.json"
    path.write_text(json.dumps(_SAMPLE), encoding="utf-8")
    return path


def test_load_present_file_returns_parsed_dict(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    loaded = le.load_latency_expectations(path)
    assert loaded is not None
    assert loaded["group_count"] == 2
    assert loaded["groups"][0]["group"] == "price_volume"


def test_load_absent_file_returns_none(tmp_path: Path) -> None:
    assert le.load_latency_expectations(tmp_path / "nope.json") is None


def test_route_serves_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    monkeypatch.setattr(le, "LATENCY_JSON_PATH", path)
    import app as dashboard_app  # noqa: E402  (path inserted above)

    client = TestClient(dashboard_app.app)
    resp = client.get("/api/latency-expectations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"][0]["group"] == "price_volume"
    assert body["e2e_context"]["target_p99_ms"] == 100


def test_route_booting_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(le, "LATENCY_JSON_PATH", tmp_path / "absent.json")
    import app as dashboard_app  # noqa: E402  (path inserted above)

    client = TestClient(dashboard_app.app)
    resp = client.get("/api/latency-expectations")
    assert resp.status_code == 503
    assert resp.json()["booting"] is True
