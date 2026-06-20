"""Persistent (Redis) cache for the LIVE feature-store GLIMPSE grid — moves the heavy build OFF the request path.

The glimpse grid (``store_glimpse.build_store_glimpse``) is a windowed-but-still-expensive read: a COLD build
of the 63-group × 30-date grid measures ~38–50s, and the per-group ticker drills add ~50s more. With only the
in-process 60s TTL (``store_glimpse.StoreGlimpseCache``), every cold/expired page load paid that full scan on
the REQUEST path — an unacceptable interactive refresh time.

This module makes the build a SCHEDULED BACKGROUND job whose result is served from a persistent cache, exactly
mirroring the ``/jobs`` pattern (``ops/collect_jobs_status.py`` precomputes on a cron → the page just reads):

  * WRITE side (``write_glimpse``) — invoked by ``ops/collect_store_glimpse.py`` on a cron (via ``docker exec``
    into THIS dashboard container, which carries quantlib/polars + the read-only ``/store`` mount + a route to
    ``quant-redis``). It builds the grid + every group's top-N ticker drill once and stores each as a JSON blob
    in Redis under stable keys (with a generous TTL so a stale-but-recent blob still serves if the worker skips
    a beat). The grid is written FIRST (the refresh-path payload) before the slower drills, so the page goes
    warm as early as possible in a run.
  * READ side (``read_glimpse`` / ``read_drill``) — called by the dashboard's ``/api/store-glimpse`` routes.
    A sub-millisecond Redis GET replaces the ~50s build, so a refresh is instant and ALWAYS warm. On a cold
    cache (worker not run yet) or an unreachable Redis the read returns ``None``; the route then serves a small
    ``warming`` placeholder rather than hanging the request on the heavy build.

Redis (the bus's ``quant-redis``) is the cache store — NOT a new piece of infrastructure: it already runs for
the feature-vector bus and is reachable from both the worker's ``docker exec`` context and the dashboard
process; ``redis`` is already a dashboard requirement (pulled by ``quantlib.bus`` since #211), so this adds NO
new third-party import and the #234 dep-closure guard stays green. Redis survives dashboard restarts and reads
a 200 KiB grid / multi-MB drill set sub-ms, which a flat JSON file would not serve as cleanly. NO schema /
format / fingerprint change to the feature store — this is a read-side cache only.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import redis

from store_glimpse import (
    DRILL_DEFAULT_LIMIT,
    GLIMPSE_DEFAULT_DAYS,
    STORE_ROOT,
    build_store_glimpse,
    build_ticker_drill,
)

# The bus Redis. quant-redis:6379 is reachable from the dashboard container and the worker's docker-exec
# context (both on the quant_default network); db 0 is the bus default. Overridable for tests / a future move.
GLIMPSE_REDIS_URL = os.environ.get("GLIMPSE_REDIS_URL", "redis://quant-redis:6379/0")

# Key namespace. The grid is one key per window-size; each drill is one key per (group, window-size). A schema
# tag in the prefix lets a future payload-shape change roll cleanly past stale blobs.
KEY_PREFIX = "dashboard:store_glimpse:v1"
GRID_KEY = f"{KEY_PREFIX}:grid:days={{days}}"
DRILL_KEY = f"{KEY_PREFIX}:drill:group={{group}}:days={{days}}:limit={{limit}}"
META_KEY = f"{KEY_PREFIX}:meta"

# Blob TTL: long enough that a missed worker beat still serves a recent grid, short enough that a permanently
# stopped worker eventually expires to a clean "warming" state rather than serving an indefinitely stale grid.
# The worker runs every few minutes; ~1h covers many missed beats.
BLOB_TTL_SECONDS = 3600


def _client(url: str = GLIMPSE_REDIS_URL) -> redis.Redis:
    """A short-timeout Redis client (decoded strings). Short timeouts so the READ path never hangs a request
    when Redis is unreachable — it fails fast and the route falls back to the warming placeholder."""
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=2.0)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_glimpse(
    root: str = STORE_ROOT,
    days: int = GLIMPSE_DEFAULT_DAYS,
    drill_limit: int = DRILL_DEFAULT_LIMIT,
    url: str = GLIMPSE_REDIS_URL,
) -> dict[str, object]:
    """Build the grid + every group's top-N ticker drill once and store each as a JSON blob in Redis.

    This is the SCHEDULED job's body (run inside the dashboard container by ``ops/collect_store_glimpse.py``).
    The grid is built and written FIRST so the refresh path warms as early as possible; the slower per-group
    drills are then built and written so a cell click is also served from cache. A group whose drill has no
    tickers in-window is still written (an empty-but-valid blob) so the reader never re-triggers a live build.

    Returns a small summary (counts + timing) for the worker to log. Lets Redis / build errors RAISE — the
    worker run fails loudly and the cron log shows it, rather than silently writing nothing.
    """
    client = _client(url)
    started = _utc_now_iso()

    grid = build_store_glimpse(root, days=days)
    client.set(GRID_KEY.format(days=days), json.dumps(grid), ex=BLOB_TTL_SECONDS)

    groups = [str(column["group"]) for column in grid["groups"]]  # type: ignore[index,union-attr]
    drills_written = 0
    for group in groups:
        drill = build_ticker_drill(group, root, days=days, limit=drill_limit)
        client.set(
            DRILL_KEY.format(group=group, days=days, limit=drill_limit),
            json.dumps(drill),
            ex=BLOB_TTL_SECONDS,
        )
        drills_written += 1

    summary = {
        "generated_at": _utc_now_iso(),
        "started_at": started,
        "days": days,
        "drill_limit": drill_limit,
        "n_groups": len(groups),
        "n_drills": drills_written,
        "anchor_date": grid.get("anchor_date"),
    }
    client.set(META_KEY, json.dumps(summary), ex=BLOB_TTL_SECONDS)
    return summary


def _warming_grid(days: int) -> dict[str, object]:
    """A small valid grid payload for the COLD-cache case: empty rows + a ``warming`` flag the page shows as
    'warming…' instead of hanging on the ~50s live build. Same shape as ``build_store_glimpse``'s empty case.
    """
    return {
        "generated_at": _utc_now_iso(),
        "store_root": STORE_ROOT,
        "anchor_date": None,
        "days": days,
        "universe_size": 0,
        "warming": True,
        "summary": {
            "n_groups": 0,
            "n_features": 0,
            "n_dates": 0,
            "n_trusted": 0,
            "trusted_pct": 0.0,
            "trust_counts": {"trusted": 0, "pending": 0, "divergent": 0, "ungraded": 0},
        },
        "groups": [],
        "dates": [],
        "cells": {},
    }


def _warming_drill(group: str, days: int, limit: int) -> dict[str, object]:
    """A small valid drill payload for the COLD-cache case (empty ticker list + ``warming`` flag)."""
    return {
        "generated_at": _utc_now_iso(),
        "group": group,
        "version": None,
        "anchor_date": None,
        "days": days,
        "warming": True,
        "dates": [],
        "n_tickers": 0,
        "limit": limit,
        "tickers": [],
    }


def read_glimpse(days: int = GLIMPSE_DEFAULT_DAYS, url: str = GLIMPSE_REDIS_URL) -> dict[str, object]:
    """Serve the precomputed grid from Redis (sub-ms). On a cold cache or unreachable Redis, return a small
    ``warming`` payload so the page shows 'warming…' rather than the route falling back to the ~50s build."""
    try:
        client = _client(url)
        blob = client.get(GRID_KEY.format(days=days))
    except redis.RedisError:
        return _warming_grid(days)
    if blob is None:
        return _warming_grid(days)
    return json.loads(blob)  # type: ignore[no-any-return]


def read_drill(
    group: str,
    days: int = GLIMPSE_DEFAULT_DAYS,
    limit: int = DRILL_DEFAULT_LIMIT,
    url: str = GLIMPSE_REDIS_URL,
) -> dict[str, object]:
    """Serve a precomputed group drill from Redis. On a cold cache / unreachable Redis, return a ``warming``
    drill payload (empty but valid) rather than building it live on the request path."""
    try:
        client = _client(url)
        blob = client.get(DRILL_KEY.format(group=group, days=days, limit=limit))
    except redis.RedisError:
        return _warming_drill(group, days, limit)
    if blob is None:
        return _warming_drill(group, days, limit)
    return json.loads(blob)  # type: ignore[no-any-return]


def main() -> None:
    """Worker entrypoint: ``python -m store_glimpse_cache`` (run inside the dashboard container by the cron).

    Precomputes the default-window grid + drills and writes them to Redis. Single window (the page's default
    30d); other windows fall back to a live build on first request, then ride the in-process TTL — the heavy,
    always-on auto-refresh path (the 30d grid) is the one this keeps warm.
    """
    summary = write_glimpse()
    print(
        "collect_store_glimpse: wrote grid + "
        f"{summary['n_drills']} drills to Redis "
        f"(days={summary['days']}, anchor={summary['anchor_date']}, "
        f"groups={summary['n_groups']})"
    )


if __name__ == "__main__":
    main()
