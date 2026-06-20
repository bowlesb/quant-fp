"""Unit tests for the append-only status store (services/dashboard/status_store).

No live DB and no live store: ``STATUS_STORE_PATH`` is pointed at a tmp file so the append-only store the host
Lead loop writes is exercised end-to-end (append_row / set_reaction / read_rows / concurrent writers). The
status DASHBOARD PAGE was removed when the dashboard was stripped to the coverage grid; the store stays as the
backing of the ops-introspection ``/api/status/rows`` read route, so only the store contract is tested here.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh status_store bound to a tmp JSON file (import-time STATUS_STORE_PATH is re-read)."""
    store_file = tmp_path / "status_dashboard.json"
    monkeypatch.setenv("STATUS_STORE_PATH", str(store_file))
    import status_store as ss

    importlib.reload(ss)
    return ss


def test_append_and_read_newest_first(store) -> None:
    store.append_row({"Latency": {"progress": "p50 1.75s", "blockers": ""}}, ts="2026-06-19T04:00:00Z")
    store.append_row(
        {"Parity": {"progress": "bb fixed", "blockers": "inf on flat"}}, ts="2026-06-19T05:00:00Z"
    )
    rows = store.read_rows()
    assert [r["ts"] for r in rows] == ["2026-06-19T05:00:00Z", "2026-06-19T04:00:00Z"]
    # normalize_cells fills both fields for every supplied workstream.
    assert rows[0]["cells"]["Parity"] == {"progress": "bb fixed", "blockers": "inf on flat"}
    assert rows[1]["cells"]["Latency"]["blockers"] == ""
    assert rows[0]["reaction"] == ""


def test_normalize_cells_defaults_missing_fields(store) -> None:
    row = store.append_row({"Lead": {"progress": "shipped #123"}})
    assert row["cells"]["Lead"] == {"progress": "shipped #123", "blockers": ""}


def test_set_reaction_round_trip_and_missing_row(store) -> None:
    store.append_row({"Modeller": {"progress": "harness armed"}}, ts="2026-06-19T05:00:00Z")
    assert store.set_reaction("2026-06-19T05:00:00Z", "nice — ship it") is True
    assert store.read_rows()[0]["reaction"] == "nice — ship it"
    # Editing the same row replaces (last write wins).
    assert store.set_reaction("2026-06-19T05:00:00Z", "hold off") is True
    assert store.read_rows()[0]["reaction"] == "hold off"
    # Unknown ts is a no-op miss.
    assert store.set_reaction("1999-01-01T00:00:00Z", "x") is False


def test_read_rows_empty_store(store) -> None:
    assert store.read_rows() == []


def test_concurrent_writers_share_one_file(store, tmp_path) -> None:
    # Two independent module instances pointed at the same file (the host Lead loop + the in-container
    # reaction POST) must each see the other's appends under the file lock.
    store.append_row({"Lead": {"progress": "a"}}, ts="2026-06-19T04:00:00Z")
    second = importlib.reload(store)
    second.append_row({"Lead": {"progress": "b"}}, ts="2026-06-19T05:00:00Z")
    assert [r["ts"] for r in store.read_rows()] == ["2026-06-19T05:00:00Z", "2026-06-19T04:00:00Z"]


def test_utc_now_iso_format(store) -> None:
    stamp = store.utc_now_iso()
    assert stamp.endswith("Z") and "T" in stamp and "+" not in stamp
