"""Per-group feature-latency EXPECTATIONS for the dashboard's latency view.

A read-side accessor for ``docs/feature_latency_expectations.json`` (#321) — the slowest-first per-group
``compute_latest`` latency profile (the live per-minute path) plus the e2e bar->vector context header. The
file is produced offline by ``quantlib.features.latency_expectations --update``; the dashboard only SERVES it.

The JSON is baked into the image at ``/app/feature_latency_expectations.json`` (see the Dockerfile), mirroring
how the curated ``feature_group_guide.yaml`` reaches the container. The path is env-overridable so a test (or a
future mount) can point elsewhere. An absent file means the dashboard is still booting (the route returns 503),
exactly like the grid's first-boot state — never a fabricated table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Baked into the image at /app/feature_latency_expectations.json (see the Dockerfile); env-overridable so a
# test or a future mount can point elsewhere.
LATENCY_JSON_PATH = Path(
    os.environ.get("FEATURE_LATENCY_JSON_PATH", "/app/feature_latency_expectations.json")
)


def load_latency_expectations(path: Path | None = None) -> dict[str, Any] | None:
    """Parse the latency-expectations JSON, or None when the file is absent (the dashboard is still booting).

    ``path`` defaults to the live module-level ``LATENCY_JSON_PATH`` (read at call time, so a test/env override
    of that attribute is honoured). A present-but-malformed file is a real defect, not a boot state, so JSON
    errors are allowed to raise rather than be masked as ``booting`` — we want to see a broken artifact loudly.
    """
    if path is None:
        path = LATENCY_JSON_PATH
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    return loaded
