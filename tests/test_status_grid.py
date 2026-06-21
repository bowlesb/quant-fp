"""Unit tests for the hourly status dashboard's append-only persistence (services/dashboard/status_grid).

The store is a single JSON file shared between the host Lead loop (write_row) and the container's reaction
POST (append_reaction); the dashboard read route serves read_grid. These tests exercise the contract the
``/api/status-grid`` routes depend on, against a tmp file (no real ~/.quant-ops):

  * write_row appends a row (all 8 columns, missing ones blank) and REPLACES the same hour idempotently;
  * a re-synthesis of an hour PRESERVES Ben's already-typed reaction;
  * append_reaction records (and clears) a reaction without touching the Lead-synthesized cells, and creates
    a placeholder row if Ben reacts to an un-synthesized hour;
  * a LEGACY ``ts``-keyed file (an earlier cycle's schema) migrates to hour-bucketed rows, carrying cells +
    reactions forward instead of crashing;
  * a missing / malformed file reads as an empty-but-valid table (first-boot, never an error).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import status_grid as sg  # noqa: E402  (path inserted above)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "status_dashboard.json"


def test_empty_on_missing_file(store: Path) -> None:
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 0
    assert grid["workstreams"] == sg.WORKSTREAMS
    assert grid["rows"] == []


def test_malformed_file_reads_empty(store: Path) -> None:
    store.write_text("{not valid json", encoding="utf-8")
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 0


def test_write_row_fills_all_columns(store: Path) -> None:
    row = sg.write_row({"Warehouse": {"progress": "built status tab", "blockers": ""}}, path=store)
    # All eight columns present; the one supplied carries its text, the rest are blank.
    assert set(row["cells"].keys()) == set(sg.WORKSTREAMS)
    assert row["cells"]["Warehouse"]["progress"] == "built status tab"
    assert row["cells"]["Latency"] == {"progress": "", "blockers": ""}


def test_write_row_replaces_same_hour_idempotently(store: Path) -> None:
    hour = "2026-06-21T20:00Z"
    sg.write_row({"Lead": {"progress": "v1", "blockers": ""}}, hour=hour, path=store)
    sg.write_row({"Lead": {"progress": "v2", "blockers": "needs Ben"}}, hour=hour, path=store)
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 1
    assert grid["rows"][0]["cells"]["Lead"]["progress"] == "v2"
    assert grid["rows"][0]["cells"]["Lead"]["blockers"] == "needs Ben"


def test_resynthesis_preserves_reaction(store: Path) -> None:
    hour = "2026-06-21T20:00Z"
    sg.write_row({"Parity": {"progress": "clean", "blockers": ""}}, hour=hour, path=store)
    sg.append_reaction(hour, "looks good, keep going", path=store)
    # The Lead re-synthesizes the SAME hour with fresh cells — Ben's reaction must survive.
    sg.write_row({"Parity": {"progress": "still clean", "blockers": ""}}, hour=hour, path=store)
    grid = sg.read_grid(store)
    assert grid["rows"][0]["reaction"] == "looks good, keep going"
    assert grid["rows"][0]["cells"]["Parity"]["progress"] == "still clean"


def test_append_reaction_then_clear(store: Path) -> None:
    hour = "2026-06-21T20:00Z"
    sg.write_row({"CD": {"progress": "ci green", "blockers": ""}}, hour=hour, path=store)
    row = sg.append_reaction(hour, "  ship it  ", path=store)
    assert row["reaction"] == "ship it"
    assert row["reaction_at"] is not None
    # Cells untouched by the reaction write.
    assert row["cells"]["CD"]["progress"] == "ci green"
    cleared = sg.append_reaction(hour, "", path=store)
    assert cleared["reaction"] == ""
    assert cleared["reaction_at"] is None


def test_reaction_on_unsynthesized_hour_creates_placeholder(store: Path) -> None:
    # Ben reacts to an hour the Lead has not written yet — the reaction must not be lost.
    row = sg.append_reaction("2026-06-21T21:00Z", "where is the modeller update?", path=store)
    assert row["reaction"] == "where is the modeller update?"
    assert set(row["cells"].keys()) == set(sg.WORKSTREAMS)
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 1


def test_rows_returned_newest_first(store: Path) -> None:
    sg.write_row({"Lead": {"progress": "older", "blockers": ""}}, hour="2026-06-21T18:00Z", path=store)
    sg.write_row({"Lead": {"progress": "newer", "blockers": ""}}, hour="2026-06-21T20:00Z", path=store)
    grid = sg.read_grid(store)
    assert [row["hour"] for row in grid["rows"]] == ["2026-06-21T20:00Z", "2026-06-21T18:00Z"]


def test_legacy_ts_rows_migrate(store: Path) -> None:
    # An earlier cycle's schema: ts-keyed rows, a "none" blocker, a reaction on one row.
    legacy = {
        "rows": [
            {
                "ts": "2026-06-19T14:51:05Z",
                "cells": {
                    "Latency": {"progress": "parked", "blockers": "none"},
                    "Modeller": {"progress": "converged", "blockers": "none"},
                },
                "reaction": "noted",
            },
            {
                "ts": "2026-06-19T15:10:00Z",
                "cells": {"Latency": {"progress": "still parked", "blockers": "none"}},
            },
        ]
    }
    store.write_text(json.dumps(legacy), encoding="utf-8")
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 2
    assert grid["workstreams"] == sg.WORKSTREAMS
    newest = grid["rows"][0]
    assert newest["hour"] == "2026-06-19T15:00Z"
    # "none" blocker migrated to empty (no blocker tag), all columns present, reaction carried forward.
    older = grid["rows"][1]
    assert older["hour"] == "2026-06-19T14:00Z"
    assert older["cells"]["Latency"]["blockers"] == ""
    assert older["reaction"] == "noted"
    assert set(older["cells"].keys()) == set(sg.WORKSTREAMS)


def test_legacy_duplicate_hours_collapse(store: Path) -> None:
    # Two legacy rows in the SAME hour collapse to one (the last write wins).
    legacy = {
        "rows": [
            {"ts": "2026-06-19T14:05:00Z", "cells": {"Lead": {"progress": "first"}}},
            {"ts": "2026-06-19T14:40:00Z", "cells": {"Lead": {"progress": "second"}}},
        ]
    }
    store.write_text(json.dumps(legacy), encoding="utf-8")
    grid = sg.read_grid(store)
    assert grid["n_rows"] == 1
    assert grid["rows"][0]["cells"]["Lead"]["progress"] == "second"
