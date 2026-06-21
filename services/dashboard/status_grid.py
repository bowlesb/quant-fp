"""The hourly status dashboard's append-only persistence (docs/OPERATING_MODEL.md §"The hourly status
dashboard").

The status dashboard is a TABLE: one ROW PER HOUR, columns = the eight workstreams
(Latency / Parity / Modeller / DataIntegrity / Maintainer / Warehouse / CD / Lead). Each cell is a concise
Progress + Blockers summary the Lead synthesizes from that workstream's ledger each cycle; each row also
carries a Ben-REACTION the Lead reviews next cycle and acts on.

PERSISTENCE — a single append-only JSON file (``STATUS_STORE_PATH``, default
``/quant-ops/status_dashboard.json``) that SURVIVES restart. The Dockerfile reserves that env and compose
bind-mounts the host's ``~/.quant-ops`` read-write, so the two writers share one file:

  * the Lead's conductor loop (running on the HOST) calls :func:`write_row` each cycle to append/replace the
    current hour's row (synthesized from the per-workstream ledgers), and reads Ben's reactions back; and
  * the dashboard container serves the table read-side (:func:`read_grid`) and appends Ben's reaction for a
    row (:func:`append_reaction`, the ``POST /api/status-grid/reaction`` handler).

The store is read-side + a single append on the request path — never a feature-store schema/format/fingerprint
change. The file is small (one compact object per hour), so each mutation does a full read-modify-write under a
short advisory lock; concurrent writers (host Lead loop + container reaction POST) are serialized by the lock so
neither clobbers the other's append.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

# The eight workstream columns, in display order. The dashboard renders exactly these as the table's columns;
# the Lead's write path supplies a {Progress, Blockers} cell per key. CD = ContinuousDeploy.
WORKSTREAMS: list[str] = [
    "Latency",
    "Parity",
    "Modeller",
    "DataIntegrity",
    "Maintainer",
    "Warehouse",
    "CD",
    "Lead",
]

# The append-only JSON store. Shared host<->container via the ~/.quant-ops bind-mount (see docker-compose.yml +
# the Dockerfile's STATUS_STORE_PATH env). A sibling ``.lock`` (derived from the store path) serializes the
# read-modify-write so the host Lead loop and the container's reaction POST never clobber each other.
STATUS_STORE_PATH = Path(os.environ.get("STATUS_STORE_PATH", "/quant-ops/status_dashboard.json"))
_LOCK_TIMEOUT_SECONDS = 5.0


def _lock_path(path: Path) -> str:
    """The advisory-lock file for a store path (a sibling ``.lock``). Derived from the actual store path so a
    write to an override path (e.g. a test tmp file) locks beside THAT file, never the default location."""
    return str(path) + ".lock"


# Schema tag in the stored payload so a future row-shape change can roll cleanly past an old file.
SCHEMA_VERSION = 1


def hour_key(moment: datetime | None = None) -> str:
    """The canonical ROW id for an hour — a UTC ``YYYY-MM-DDTHH:00Z`` bucket. One row per hour: a given hour's
    row is replaced (not duplicated) when re-synthesized, keyed by this."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:00Z")


def _empty_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "workstreams": WORKSTREAMS, "rows": []}


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a stored row into the current shape. Migrates a legacy ``ts``-keyed row (an earlier cycle's
    schema) to an ``hour``-bucketed row, preserving its cells + reaction, so a pre-existing history is carried
    forward rather than lost or crashed on. Idempotent for already-current rows."""
    raw_hour = row.get("hour")
    if not raw_hour and row.get("ts"):
        moment = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        raw_hour = hour_key(moment)
    if not raw_hour:
        raw_hour = hour_key()
    cells_raw = row.get("cells", {})
    cells: dict[str, dict[str, str]] = {}
    for workstream in WORKSTREAMS:
        cell = cells_raw.get(workstream, {}) if isinstance(cells_raw, dict) else {}
        progress = str(cell.get("progress", "")).strip()
        blockers = str(cell.get("blockers", "")).strip()
        # A legacy "none" blocker means no blocker — render it as empty so the cell shows no blocker tag.
        if blockers.lower() == "none":
            blockers = ""
        cells[workstream] = {"progress": progress, "blockers": blockers}
    reaction = str(row.get("reaction", "")).strip()
    return {
        "hour": raw_hour,
        "created_at": row.get("created_at") or row.get("ts") or raw_hour,
        "updated_at": row.get("updated_at") or row.get("ts") or raw_hour,
        "cells": cells,
        "reaction": reaction,
        "reaction_at": row.get("reaction_at") or (row.get("ts") if reaction else None),
    }


def _load(path: Path) -> dict[str, Any]:
    """Read the store, returning an empty-but-valid shape when the file is absent (first-ever boot) or empty.
    A malformed file is treated as empty rather than crashing the read path — the Lead loop's next write heals
    it (the rows it would have appended come from the durable ledgers, not this file)."""
    if not path.exists() or path.stat().st_size == 0:
        return _empty_store()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return _empty_store()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _empty_store()
    data.setdefault("schema_version", SCHEMA_VERSION)
    data["workstreams"] = WORKSTREAMS
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    # Migrate every row to the current shape (legacy ts-keyed rows -> hour-bucketed); collapse any duplicate
    # hours to the last write for that hour (one row per hour).
    by_hour: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        normalized = _normalize_row(raw_row)
        by_hour[normalized["hour"]] = normalized
    data["rows"] = list(by_hour.values())
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write the store atomically (temp file + rename) so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_grid(path: Path = STATUS_STORE_PATH) -> dict[str, Any]:
    """The full status table for the UI — workstream columns + every hourly row (newest first). Read-only; no
    lock needed (a torn read is impossible because writes are atomic-rename). Returns an empty-but-valid shape
    on first boot so the UI renders an empty table rather than an error."""
    data = _load(path)
    rows = sorted(data["rows"], key=lambda row: row["hour"], reverse=True)
    return {
        "schema_version": data["schema_version"],
        "workstreams": data["workstreams"],
        "rows": rows,
        "n_rows": len(rows),
    }


def write_row(
    cells: dict[str, dict[str, str]],
    hour: str | None = None,
    path: Path = STATUS_STORE_PATH,
) -> dict[str, Any]:
    """Append OR replace the row for an hour — the Lead conductor loop's write path (called on the HOST each
    cycle with cells synthesized from the per-workstream ledgers).

    ``cells`` maps each workstream key (a subset/all of :data:`WORKSTREAMS`) to a ``{"progress": str,
    "blockers": str}`` cell; missing workstreams are filled with an empty cell so every row has all columns.
    A row for ``hour`` (default: the current UTC hour) is REPLACED in place if it exists (re-synthesis is
    idempotent), else appended. Ben's already-typed ``reaction`` for that hour is PRESERVED across a
    re-synthesis (the Lead refreshing the status text must never wipe a reaction Ben left). Returns the
    written row.
    """
    hour = hour or hour_key()
    normalized: dict[str, dict[str, str]] = {}
    for workstream in WORKSTREAMS:
        cell = cells.get(workstream, {})
        normalized[workstream] = {
            "progress": str(cell.get("progress", "")).strip(),
            "blockers": str(cell.get("blockers", "")).strip(),
        }
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with FileLock(_lock_path(path), timeout=_LOCK_TIMEOUT_SECONDS):
        data = _load(path)
        rows: list[dict[str, Any]] = data["rows"]
        existing = next((row for row in rows if row["hour"] == hour), None)
        if existing is not None:
            existing["cells"] = normalized
            existing["updated_at"] = now_iso
            row = existing
        else:
            row = {
                "hour": hour,
                "created_at": now_iso,
                "updated_at": now_iso,
                "cells": normalized,
                "reaction": "",
                "reaction_at": None,
            }
            rows.append(row)
        data["workstreams"] = WORKSTREAMS
        _atomic_write(path, data)
        return row


def append_reaction(hour: str, reaction: str, path: Path = STATUS_STORE_PATH) -> dict[str, Any]:
    """Record Ben's reaction for an hour's row — the ``POST /api/status-grid/reaction`` handler. The reaction
    REPLACES any prior reaction on that row (Ben edits his own note) and stamps the time; the Lead reviews
    these every cycle. Creates a minimal placeholder row if Ben reacts to an hour the Lead hasn't synthesized
    yet (so a reaction is never lost). Returns the updated row."""
    cleaned = reaction.strip()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with FileLock(_lock_path(path), timeout=_LOCK_TIMEOUT_SECONDS):
        data = _load(path)
        rows: list[dict[str, Any]] = data["rows"]
        row = next((candidate for candidate in rows if candidate["hour"] == hour), None)
        if row is None:
            row = {
                "hour": hour,
                "created_at": now_iso,
                "updated_at": now_iso,
                "cells": {workstream: {"progress": "", "blockers": ""} for workstream in WORKSTREAMS},
                "reaction": "",
                "reaction_at": None,
            }
            rows.append(row)
        row["reaction"] = cleaned
        row["reaction_at"] = now_iso if cleaned else None
        _atomic_write(path, data)
        return row
