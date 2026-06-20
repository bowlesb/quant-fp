"""Reader for the jobs-status JSON the host collector writes (the ops ``/api/jobs`` read route's source).

``~/.quant-ops/jobs_status.json`` is written on the host by ``ops/collect_jobs_status.py`` (scheduled crons +
running job containers + recent runs) and mounted into the dashboard container at ``/quant-ops``. The dashboard
no longer renders a ``/jobs`` PAGE — only the ``/api/jobs`` read route serves this file for ops visibility — so
this module is now just the loader (the server-rendered page was dropped when the dashboard was stripped to the
coverage grid). The collector is the only writer; this only reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _default_jobs_path() -> Path:
    """The jobs-status file path. An explicit ``JOBS_STATUS_PATH`` wins, else derive it from
    ``STATUS_STORE_PATH``'s directory (the collector writes jobs_status.json beside status_dashboard.json),
    else the default mount path."""
    explicit = os.environ.get("JOBS_STATUS_PATH")
    if explicit:
        return Path(explicit)
    status_path = os.environ.get("STATUS_STORE_PATH")
    if status_path:
        return Path(status_path).parent / "jobs_status.json"
    return Path("/quant-ops/jobs_status.json")


JOBS_STATUS_PATH = _default_jobs_path()


def load_status() -> dict[str, Any] | None:
    """Read jobs_status.json; ``None`` if missing/empty/corrupt so the ``/api/jobs`` route can return an
    empty-but-valid shape rather than erroring."""
    if not JOBS_STATUS_PATH.exists():
        return None
    text = JOBS_STATUS_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data
