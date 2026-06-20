"""Always-warm Redis cache + background WORKER for the ticker×date coverage matrix.

The matrix build (``store_grid.build_store_grid``) is a full-store pass (~3-4min: every in-window partition
pays one bounded symbol read, dominated by the deep calendar groups) — far too slow for a request, but fine
on a permanent background loop that always serves the last-good blob. This module is that loop + the read side:

  * WORKER (``run_forever``) — the ``store-glimpse-worker`` container's entrypoint. Build the matrix once on
    boot, write it to Redis, then rebuild every ``STORE_GRID_INTERVAL_SECONDS`` forever. Each successful loop
    REFRESHES a long TTL on the blob, so the cache is effectively last-good-forever: a stopped/slow worker
    keeps serving the last good matrix instead of decaying to a "warming" placeholder. The ONLY time a reader
    sees no data is the genuine first-ever boot before the first build lands (seconds).

  * READ side (``read_grid`` / ``read_drill`` / ``read_meta``) — the dashboard routes. A sub-ms Redis GET of
    the gzipped blob. The grid blob is stored gzip-compressed (a dense ~2.8M-cell matrix is multi-MB as raw
    JSON; gzip takes it to a few hundred KB) and the matrix route serves those exact bytes with
    ``Content-Encoding: gzip`` — no decompress/recompress on the request path. ``read_meta`` returns the small
    generated-at / dims header (decompressed) so the UI can show "as of HH:MM:SS" staleness.

Redis (the bus's ``quant-redis``) is the store — already running for the feature-vector bus and reachable
from both the worker and the dashboard on ``quant_default``; ``redis`` is already a dashboard requirement, so
NO new third-party import. NO feature-store schema/format/fingerprint change — read-side only.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import time

import redis

from store_grid import (
    GRID_LOOKBACK_DAYS,
    STORE_ROOT,
    build_store_grid,
    build_ticker_drill,
    gather_window,
)

logger = logging.getLogger("store_grid")

# The bus Redis, reachable from the worker + dashboard on quant_default. Overridable for tests / a future move.
GRID_REDIS_URL = os.environ.get("STORE_GRID_REDIS_URL", "redis://quant-redis:6379/0")

# Key namespace. A schema tag in the prefix lets a future payload-shape change roll cleanly past stale blobs.
KEY_PREFIX = "dashboard:store_grid:v1"
GRID_KEY = f"{KEY_PREFIX}:matrix"
DRILL_KEY = f"{KEY_PREFIX}:drill:{{symbol}}"
META_KEY = f"{KEY_PREFIX}:meta"

# Blob TTL. Long (24h) and REFRESHED every successful loop, so a missed/slow/stopped worker keeps serving the
# last good matrix rather than expiring to nothing. Only a worker down for a full day lets the cache lapse.
BLOB_TTL_SECONDS = int(os.environ.get("STORE_GRID_TTL_SECONDS", str(24 * 3600)))

# Worker loop cadence: the gap to WAIT AFTER each build before the next one. A full 18-month rebuild measures
# ~3-4min (every in-window partition pays one bounded symbol read), and the store/trust data changes slowly
# (capture is per-session, trust per-sweep), so a few-minute gap keeps the grid fresh without pinning the disk.
# Sleeping AFTER the build (not racing a fixed wall-clock period) means builds never pile up however long they
# take — the worker is always either building or sleeping this gap.
INTERVAL_SECONDS = int(os.environ.get("STORE_GRID_INTERVAL_SECONDS", "180"))

# Drills are lazy (per ticker, on a column click), but the worker pre-warms the most-covered N so the common
# clicks are instant; an un-warmed ticker falls back to a live build on the route (one ticker is cheap).
DRILL_PREWARM_TOP_N = int(os.environ.get("STORE_GRID_DRILL_PREWARM", "100"))


def _client(url: str = GRID_REDIS_URL) -> redis.Redis:
    """A short-timeout Redis client over BYTES (the grid blob is gzip). Short timeouts so the READ path never
    hangs a request when Redis is unreachable — it fails fast and the route reports an empty/booting state.
    """
    return redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1.0, socket_timeout=2.0)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_grid(
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    drill_prewarm: int = DRILL_PREWARM_TOP_N,
    url: str = GRID_REDIS_URL,
) -> dict[str, object]:
    """Build the matrix + pre-warm the top-N ticker drills, write each to Redis (the grid gzip-compressed),
    refresh the TTL. Gathers the store ONCE (``gather_window``) and reuses it for the matrix AND every drill,
    so pre-warming N drills costs no extra store I/O. Returns a small summary for the worker to log. Lets
    Redis/build errors RAISE so a failed loop is loud in the worker log rather than silently writing nothing.
    """
    client = _client(url)
    started = time.monotonic()

    window = gather_window(root, lookback_days)
    grid = build_store_grid(root, lookback_days=lookback_days, window_data=window)
    raw = json.dumps(grid).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=6)
    client.set(GRID_KEY, blob, ex=BLOB_TTL_SECONDS)

    tickers = list(grid["tickers"])  # type: ignore[arg-type]
    prewarmed = 0
    for symbol in tickers[: max(0, drill_prewarm)]:
        drill = build_ticker_drill(str(symbol), root, lookback_days=lookback_days, window_data=window)
        client.set(
            DRILL_KEY.format(symbol=str(symbol)),
            gzip.compress(json.dumps(drill).encode("utf-8"), compresslevel=6),
            ex=BLOB_TTL_SECONDS,
        )
        prewarmed += 1

    summary = {
        "generated_at": grid["generated_at"],
        "anchor_date": grid["anchor_date"],
        "lookback_days": grid["lookback_days"],
        "n_dates": grid["summary"]["n_dates"],  # type: ignore[index]
        "n_tickers": grid["summary"]["n_tickers"],  # type: ignore[index]
        "n_groups": grid["summary"]["n_groups"],  # type: ignore[index]
        "n_trusted_groups": grid["summary"]["n_trusted_groups"],  # type: ignore[index]
        "mean_coverage_pct": grid["summary"]["mean_coverage_pct"],  # type: ignore[index]
        "raw_bytes": len(raw),
        "gzip_bytes": len(blob),
        "drills_prewarmed": prewarmed,
        "build_seconds": round(time.monotonic() - started, 1),
    }
    client.set(META_KEY, json.dumps(summary).encode("utf-8"), ex=BLOB_TTL_SECONDS)
    return summary


def read_grid_gzip(url: str = GRID_REDIS_URL) -> bytes | None:
    """The gzip-compressed matrix blob exactly as stored (the route serves it with Content-Encoding: gzip).
    ``None`` on a cold cache (first-ever boot, no blob yet) or unreachable Redis — the route reports booting.
    """
    try:
        client = _client(url)
        return client.get(GRID_KEY)  # type: ignore[return-value]
    except redis.RedisError:
        logger.warning("store_grid: Redis unreachable on grid read")
        return None


def read_meta(url: str = GRID_REDIS_URL) -> dict[str, object] | None:
    """The small meta header (generated_at, dims) for the UI's "as of" staleness. ``None`` if not built yet."""
    try:
        client = _client(url)
        blob = client.get(META_KEY)
    except redis.RedisError:
        logger.warning("store_grid: Redis unreachable on meta read")
        return None
    if blob is None:
        return None
    return json.loads(blob)  # type: ignore[no-any-return]


def read_drill(symbol: str, root: str = STORE_ROOT, url: str = GRID_REDIS_URL) -> dict[str, object]:
    """One ticker's per-(date×group) presence drill. Served from the pre-warmed blob when available; an
    un-warmed ticker (outside the top-N) falls back to a live build — a single ticker is cheap (~1s)."""
    symbol = symbol.upper()
    try:
        client = _client(url)
        blob = client.get(DRILL_KEY.format(symbol=symbol))
    except redis.RedisError:
        blob = None
    if blob is not None:
        return json.loads(gzip.decompress(blob))  # type: ignore[no-any-return]
    return build_ticker_drill(symbol, root)


def run_forever(
    root: str = STORE_ROOT,
    interval_seconds: int = INTERVAL_SECONDS,
    url: str = GRID_REDIS_URL,
) -> None:
    """The worker's permanent loop: build the matrix on boot, then rebuild + refresh the TTL, then wait
    ``interval_seconds`` and repeat — forever. The wait is AFTER the build (not a fixed wall-clock period), so
    however long a build takes, the next one never piles up: the worker is always either building or sleeping
    the gap. A failed loop is logged and the loop continues (the last-good blob keeps serving via its TTL); it
    does not crash the worker. Designed for a ``restart: unless-stopped`` container."""
    logger.info(
        "store_grid worker starting: store=%s interval=%ss ttl=%ss",
        root,
        interval_seconds,
        BLOB_TTL_SECONDS,
    )
    while True:
        try:
            summary = write_grid(root=root, url=url)
            logger.info(
                "store_grid: wrote matrix %sx%s (%s groups, %s trusted) anchor=%s "
                "gzip=%.0fKB drills=%s in %.1fs",
                summary["n_dates"],
                summary["n_tickers"],
                summary["n_groups"],
                summary["n_trusted_groups"],
                summary["anchor_date"],
                int(summary["gzip_bytes"]) / 1024,  # type: ignore[call-overload]
                summary["drills_prewarmed"],
                summary["build_seconds"],
            )
        except redis.RedisError as exc:
            logger.error("store_grid: Redis error this loop, keeping last-good: %s", exc)
        time.sleep(max(1.0, interval_seconds))


def main() -> None:
    """Worker entrypoint: ``python -m store_grid_cache`` (the store-glimpse-worker container CMD)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
