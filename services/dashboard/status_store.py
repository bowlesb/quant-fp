"""Append-only persistence for the hourly STATUS DASHBOARD.

The Lead's loop synthesizes one ROW per hour — a per-workstream snapshot of Progress + Blockers — and
Ben types a free-text REACTION onto a row from his browser. Both writers (the Lead loop on the host, the
FastAPI ``POST /api/status/reaction`` handler in the dashboard container) share this module so the on-disk
schema has a single source of truth.

Storage: a single JSON file (``STATUS_STORE_PATH``, default ``~/.quant-ops/status_dashboard.json``) holding
``{"rows": [row, ...]}`` with the NEWEST row last on disk (the page reverses it to newest-first). A row::

    {
      "ts": "2026-06-19T05:00:00Z",          # ISO-8601 UTC, the hourly snapshot timestamp + row id
      "cells": {                              # one entry per workstream column
        "Latency":       {"progress": "...", "blockers": ""},
        "Parity":        {"progress": "...", "blockers": "bb_position inf on flat windows"},
        ...
      },
      "reaction": ""                          # Ben's free text for this row ("" until he types one)
    }

The two host/container writers can race, so every mutation takes a coarse file lock (``fcntl.flock``) over a
sidecar lockfile and does a read-modify-write under it. Reads are lock-free (a torn read just renders a
slightly stale page; the next refresh fixes it).

The Lead loop calls :func:`append_row` once per cycle; Ben's browser POST calls :func:`set_reaction`.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any

# The seven workstream columns, in display order. Extensible: add a name here and the page grows a column;
# older rows without that key render as an empty cell (see ``cell_for``).
WORKSTREAMS = [
    "Latency",
    "Parity",
    "Modeller",
    "DataIntegrity",
    "Warehouse",
    "Maintainer",
    "Lead",
]

STATUS_STORE_PATH = Path(
    os.environ.get("STATUS_STORE_PATH", str(Path.home() / ".quant-ops" / "status_dashboard.json"))
)


def _lock_path() -> Path:
    return STATUS_STORE_PATH.with_suffix(STATUS_STORE_PATH.suffix + ".lock")


def utc_now_iso() -> str:
    """Current time as a second-precision ISO-8601 UTC string (the row id format)."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_unlocked() -> dict[str, Any]:
    if not STATUS_STORE_PATH.exists():
        return {"rows": []}
    text = STATUS_STORE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {"rows": []}
    data: dict[str, Any] = json.loads(text)
    if "rows" not in data:
        data["rows"] = []
    return data


def _write_unlocked(data: dict[str, Any]) -> None:
    """Atomic replace: write a sibling temp file then ``os.replace`` so a reader never sees a half-written
    file. The caller holds the flock, so the temp-name collision window is irrelevant."""
    STATUS_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_STORE_PATH.with_suffix(STATUS_STORE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(tmp, STATUS_STORE_PATH)


def normalize_cells(cells: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Coerce a caller-supplied cells dict into the canonical ``{workstream: {progress, blockers}}`` shape.

    Unknown workstream keys are kept (the page tolerates extra columns), and each cell is normalized to have
    both ``progress`` and ``blockers`` string fields so the renderer never has to guess.
    """
    normalized: dict[str, dict[str, str]] = {}
    for workstream, fields in cells.items():
        progress = str(fields["progress"]) if "progress" in fields else ""
        blockers = str(fields["blockers"]) if "blockers" in fields else ""
        normalized[workstream] = {"progress": progress, "blockers": blockers}
    return normalized


def read_rows() -> list[dict[str, Any]]:
    """All rows NEWEST-FIRST (the page render order). Lock-free."""
    rows: list[dict[str, Any]] = list(_read_unlocked()["rows"])
    rows.reverse()
    return rows


def append_row(cells: dict[str, dict[str, str]], ts: str | None = None) -> dict[str, Any]:
    """Append one hourly snapshot row and return it. Called by the Lead loop once per cycle.

    ``cells`` is ``{workstream: {"progress": ..., "blockers": ...}}`` — only the workstreams the Lead has
    something to say about need be present. ``ts`` defaults to now (UTC); pass it to backfill/seed a row.
    """
    row = {"ts": ts or utc_now_iso(), "cells": normalize_cells(cells), "reaction": ""}
    with open(_lock_path(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_unlocked()
        data["rows"].append(row)
        _write_unlocked(data)
    return row


def set_reaction(ts: str, text: str) -> bool:
    """Set Ben's reaction text on the row identified by ``ts``. Returns False if no such row exists.

    Called by the dashboard's ``POST /api/status/reaction`` handler. Last write wins (Ben editing the same
    row replaces the prior text).
    """
    with open(_lock_path(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_unlocked()
        for row in data["rows"]:
            if row["ts"] == ts:
                row["reaction"] = text
                _write_unlocked(data)
                return True
    return False
