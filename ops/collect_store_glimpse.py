#!/usr/bin/env python3
"""Host-side cron wrapper that precomputes the /store-glimpse grid into the persistent (Redis) cache.

A scheduled collector that precomputes a dashboard payload off the request path — but the glimpse build
needs ``quantlib`` + ``polars`` + the ``/store`` mount, none of which
the HOST python carries. So, exactly like ``ops/healthcheck.sh`` execs ``python -m quantlib.ops.healthcheck``
inside the ``feature-computer`` container, this wrapper execs ``python -m store_glimpse_cache`` inside the
``quant-dashboard-1`` container (which has those deps + a read-only ``/store`` + a route to ``quant-redis``).
That module builds the 63-group × 30-date grid + every group's top-N ticker drill and writes each as a JSON
blob to Redis; the dashboard's ``/api/store-glimpse`` routes then serve sub-ms from those blobs.

Read-only w.r.t. the feature store (the container mounts ``/store`` read-only) and idempotent: each run just
overwrites the same Redis keys with a fresh blob (a generous TTL keeps a recent blob serving if a beat is
missed). Run on a cron every few minutes — the store/trust data changes slowly, so a few-minute staleness is
fine and the ~100s build never touches an interactive request. The wrapper itself only shells out to
``docker exec``; it never connects to Redis or reads the store directly.
"""
from __future__ import annotations

import os
import subprocess
import sys

# The dashboard container that carries quantlib/polars + the read-only /store mount + a route to quant-redis.
# Overridable so a renamed/recomposed container is a config change, not a code edit.
DASHBOARD_CONTAINER = os.environ.get("GLIMPSE_DASHBOARD_CONTAINER", "quant-dashboard-1")

# The in-container module that builds the grid + drills and writes them to Redis (see
# services/dashboard/store_glimpse_cache.py, COPY'd to /app in the dashboard image).
GLIMPSE_MODULE = "store_glimpse_cache"

# Generous wall-clock bound: a full cold build (grid ~50s + all drills ~55s) is ~105s; allow headroom so a
# slow disk run is not killed mid-write, but a truly hung exec cannot pile up across cron beats.
EXEC_TIMEOUT_SECONDS = 240


def run() -> int:
    """Exec the in-container precompute; return its exit code. A non-zero code / timeout / missing-docker is
    reported on stderr (the cron log) so a silently-stale cache is visible, not hidden."""
    cmd = [
        "docker",
        "exec",
        "-e",
        "PYTHONPATH=/app",
        DASHBOARD_CONTAINER,
        "python3",
        "-m",
        GLIMPSE_MODULE,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=EXEC_TIMEOUT_SECONDS,
        check=False,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        sys.stderr.write(
            f"collect_store_glimpse: docker exec into {DASHBOARD_CONTAINER} exited "
            f"{result.returncode}\n{result.stderr.strip()}\n"
        )
    return result.returncode


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
