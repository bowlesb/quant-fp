"""Append-only TIME-SERIES persistence for the SYSTEM PROGRESS scorecard.

The scorecard (``scorecard.py``) computes the CURRENT value of Ben's six platform axes (A trusted / B
deployed / C trust-process health / D latency / E raw-coverage / F open issues) from the existing tables,
manifests and ledgers. To make the TRAJECTORY legible — not reconstructed from scratch each time — we persist
a small SNAPSHOT of those headline numbers each time the scorecard is built, and the panel draws a sparkline
from the snapshot history.

This deliberately mirrors ``status_store.py`` (the hourly status board's append-only JSON): one JSON file
holding ``{"snapshots": [snap, ...]}`` with the NEWEST snapshot last on disk, a coarse ``fcntl.flock`` over a
sidecar lockfile around every mutation (the host Lead loop and the FastAPI container can both append), and an
atomic temp-file replace so a reader never sees a half-written file. NO DB table, NO schema change — the same
JSON-snapshot pattern the status board already uses.

A snapshot is just the headline scalar of each axis plus a timestamp::

    {
      "ts": "2026-06-19T16:00:00Z",     # ISO-8601 UTC, second precision — the snapshot id
      "axes": {
        "A_trusted":        {"value": 106,  "pct": 15.5},
        "B_deployed":       {"value": 694,  "groups": 56},
        "C_process_health": {"eligible": 532, "blocked": 56, "open_defects": 56},
        "D_latency":        {"p50_ms": 401, "p99_ms": 761},
        "E_raw_coverage":   {"bars_span_days": 899, "trades_symbols_day": 1900, "quotes_span_days": 90},
        "F_open_issues":    {"open_defects": 56, "open_prs": 1, "quarantined": 56}
      }
    }

To keep the file from growing without bound over months of snapshots, appends DE-DUPE on the same UTC minute
(a busy refresh re-hitting the endpoint within a minute replaces that minute's snapshot rather than stacking
duplicates — so the trend is one point per distinct minute, not per page-load). History is otherwise kept in
full: the whole point is the long trajectory.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any

SCORECARD_STORE_PATH = Path(
    os.environ.get("SCORECARD_STORE_PATH", str(Path.home() / ".quant-ops" / "scorecard_snapshots.json"))
)


def _lock_path() -> Path:
    return SCORECARD_STORE_PATH.with_suffix(SCORECARD_STORE_PATH.suffix + ".lock")


def utc_now_iso() -> str:
    """Current time as a second-precision ISO-8601 UTC string (the snapshot id format)."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _minute_key(ts: str) -> str:
    """The UTC-minute bucket of a snapshot id, used for de-dupe (two appends in the same minute collapse)."""
    return ts[:16]  # "YYYY-MM-DDTHH:MM"


def _read_unlocked() -> dict[str, Any]:
    if not SCORECARD_STORE_PATH.exists():
        return {"snapshots": []}
    text = SCORECARD_STORE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {"snapshots": []}
    data: dict[str, Any] = json.loads(text)
    if "snapshots" not in data:
        data["snapshots"] = []
    return data


def _write_unlocked(data: dict[str, Any]) -> None:
    """Atomic replace: write a sibling temp file then ``os.replace`` so a reader never sees a half-written
    file. The caller holds the flock, so the temp-name collision window is irrelevant."""
    SCORECARD_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCORECARD_STORE_PATH.with_suffix(SCORECARD_STORE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(tmp, SCORECARD_STORE_PATH)


def read_snapshots() -> list[dict[str, Any]]:
    """All snapshots OLDEST-FIRST (chronological — the sparkline draws left-to-right). Lock-free read (a torn
    read just renders a slightly stale trend; the next refresh fixes it)."""
    return list(_read_unlocked()["snapshots"])


def append_snapshot(axes: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
    """Append one scorecard snapshot and return it. Called whenever the scorecard is built (the endpoint, or a
    small helper the Lead loop can call).

    ``axes`` is the per-axis headline-scalar dict (see module docstring). ``ts`` defaults to now (UTC); pass it
    to seed/backfill. De-dupes on the UTC minute: if a snapshot already exists for this minute it is REPLACED
    (so a busy refresh does not stack duplicate points), otherwise the snapshot is appended."""
    snap = {"ts": ts or utc_now_iso(), "axes": axes}
    minute = _minute_key(snap["ts"])
    with open(_lock_path(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_unlocked()
        snaps = data["snapshots"]
        for index, existing in enumerate(snaps):
            if _minute_key(existing["ts"]) == minute:
                snaps[index] = snap
                _write_unlocked(data)
                return snap
        snaps.append(snap)
        _write_unlocked(data)
    return snap
