"""Always-warm MongoDB cache + background WORKER for the ticker×date coverage matrix.

The matrix build (``store_grid.build_store_grid``) is a full-store pass (~3-4min: every in-window partition
pays one bounded symbol read, dominated by the deep calendar groups) — far too slow for a request, but fine
on a permanent background loop that always serves the last-good document. This module is that loop + the read
side:

  * WORKER (``run_forever``) — the ``store-grid-worker`` container's entrypoint. Build the matrix once on boot,
    write it to Mongo, then rebuild every ``STORE_GRID_INTERVAL_SECONDS`` (default 10 MIN) forever. Each loop
    OVERWRITES the cache document, so the cache is last-good-forever: a stopped/slow worker keeps serving the
    last good document (Mongo has no TTL on these docs — they persist until the next successful build replaces
    them). The ONLY time a reader sees no data is the genuine first-ever boot before the first build lands.

  * READ side (``read_grid_gzip`` / ``read_drill`` / ``read_meta``) — the dashboard routes. A single indexed
    Mongo lookup. The grid is stored gzip-compressed (a dense ~2.8M-cell matrix is multi-MB as raw JSON; gzip
    takes it to a few hundred KB) as binary in the document, and the matrix route serves those exact bytes with
    ``Content-Encoding: gzip`` — no decompress/recompress on the request path. ``read_meta`` returns the small
    generated-at / dims header so the UI can show "as of HH:MM:SS" staleness.

MongoDB is a dedicated ``mongo`` compose service (NOT the feature pipeline's store): the worker WRITES the
precomputed grid there and the dashboard READS it, so a page refresh is one indexed document fetch and the
heavy build is never on the request path. ``pymongo`` is the only client dep. NO feature-store schema/format/
fingerprint change — this is a read-side cache only, and the feature store is mounted READ-ONLY in the worker.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from store_grid import (
    GRID_LOOKBACK_DAYS,
    STORE_ROOT,
    build_store_grid,
    build_ticker_drill,
    gather_window,
)

logger = logging.getLogger("store_grid")

# The dedicated cache Mongo (the ``mongo`` compose service on quant_default). Overridable for tests / a move.
GRID_MONGO_URL = os.environ.get("STORE_GRID_MONGO_URL", "mongodb://quant-mongo:27017")
GRID_DB_NAME = os.environ.get("STORE_GRID_MONGO_DB", "dashboard")

# Collections. A schema tag in the doc ``_id`` / collection names lets a future payload-shape change roll
# cleanly past a stale document. The grid is one doc (_id="matrix"); meta one doc (_id="meta"); drills one doc
# per symbol (_id=<SYMBOL>).
GRID_COLLECTION = "store_grid_matrix"
DRILL_COLLECTION = "store_grid_drill"
META_COLLECTION = "store_grid_meta"
GRID_DOC_ID = "matrix"
META_DOC_ID = "meta"

# Worker loop cadence: the gap to WAIT AFTER each build before the next one. Ben's spec is a 10-MINUTE refresh.
# The store/trust data changes slowly (capture per-session, trust per-sweep), so 10 min is plenty fresh; the
# wait is AFTER the build so a long (~3-4min) build never piles up — the worker is always building or sleeping.
INTERVAL_SECONDS = int(os.environ.get("STORE_GRID_INTERVAL_SECONDS", "600"))

# Drills are lazy (per ticker, on a column click), but the worker pre-warms the most-covered N so the common
# clicks are instant; an un-warmed ticker falls back to a live build on the route (one ticker is cheap).
DRILL_PREWARM_TOP_N = int(os.environ.get("STORE_GRID_DRILL_PREWARM", "100"))

# Short timeouts so the READ path never hangs a request when Mongo is unreachable — it fails fast and the route
# reports a booting state. The worker uses the same client; a write failure raises and the loop logs + retries.
_CONNECT_TIMEOUT_MS = 1500
_SERVER_SELECT_TIMEOUT_MS = 1500


def _client(url: str = GRID_MONGO_URL) -> MongoClient:
    """A short-timeout Mongo client. Short server-selection so the read path fails fast when Mongo is down."""
    return MongoClient(
        url,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        serverSelectionTimeoutMS=_SERVER_SELECT_TIMEOUT_MS,
    )


def write_grid(
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    drill_prewarm: int = DRILL_PREWARM_TOP_N,
    url: str = GRID_MONGO_URL,
) -> dict[str, object]:
    """Build the matrix + pre-warm the top-N ticker drills and UPSERT each into Mongo (the grid + drills stored
    gzip-compressed as binary). Gathers the store ONCE (``gather_window``) and reuses it for the matrix AND
    every drill, so pre-warming N drills costs no extra store I/O. Returns a small summary for the worker to
    log. Lets Mongo / build errors RAISE so a failed loop is loud in the worker log rather than silently
    writing nothing."""
    client = _client(url)
    database = client[GRID_DB_NAME]
    started = time.monotonic()

    window = gather_window(root, lookback_days)
    grid = build_store_grid(root, lookback_days=lookback_days, window_data=window)
    raw = json.dumps(grid).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=6)

    database[GRID_COLLECTION].replace_one(
        {"_id": GRID_DOC_ID},
        {"_id": GRID_DOC_ID, "gzip": blob, "generated_at": grid["generated_at"]},
        upsert=True,
    )

    tickers = list(grid["tickers"])  # type: ignore[arg-type]
    prewarmed = 0
    drill_collection = database[DRILL_COLLECTION]
    for symbol in tickers[: max(0, drill_prewarm)]:
        drill = build_ticker_drill(str(symbol), root, lookback_days=lookback_days, window_data=window)
        drill_collection.replace_one(
            {"_id": str(symbol)},
            {"_id": str(symbol), "gzip": gzip.compress(json.dumps(drill).encode("utf-8"), compresslevel=6)},
            upsert=True,
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
    database[META_COLLECTION].replace_one({"_id": META_DOC_ID}, {"_id": META_DOC_ID, **summary}, upsert=True)
    return summary


def read_grid_gzip(url: str = GRID_MONGO_URL) -> bytes | None:
    """The gzip-compressed matrix blob exactly as stored (the route serves it with Content-Encoding: gzip).
    ``None`` on a cold cache (first-ever boot, no document yet) or unreachable Mongo — the route reports
    booting."""
    try:
        client = _client(url)
        doc = client[GRID_DB_NAME][GRID_COLLECTION].find_one({"_id": GRID_DOC_ID})
    except PyMongoError:
        logger.warning("store_grid: Mongo unreachable on grid read")
        return None
    if doc is None:
        return None
    return bytes(doc["gzip"])


def read_meta(url: str = GRID_MONGO_URL) -> dict[str, object] | None:
    """The small meta header (generated_at, dims) for the UI's "as of" staleness. ``None`` if not built yet.
    The Mongo ``_id`` is stripped so the payload is exactly the summary the worker wrote."""
    try:
        client = _client(url)
        doc = client[GRID_DB_NAME][META_COLLECTION].find_one({"_id": META_DOC_ID})
    except PyMongoError:
        logger.warning("store_grid: Mongo unreachable on meta read")
        return None
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


def read_drill(symbol: str, root: str = STORE_ROOT, url: str = GRID_MONGO_URL) -> dict[str, object]:
    """One ticker's per-(date×group) presence drill. Served from the pre-warmed document when available; an
    un-warmed ticker (outside the top-N) falls back to a live build — a single ticker is cheap (~1s)."""
    symbol = symbol.upper()
    try:
        client = _client(url)
        doc = client[GRID_DB_NAME][DRILL_COLLECTION].find_one({"_id": symbol})
    except PyMongoError:
        doc = None
    if doc is not None:
        return json.loads(gzip.decompress(bytes(doc["gzip"])))  # type: ignore[no-any-return]
    return build_ticker_drill(symbol, root)


def run_forever(
    root: str = STORE_ROOT,
    interval_seconds: int = INTERVAL_SECONDS,
    url: str = GRID_MONGO_URL,
) -> None:
    """The worker's permanent loop: build the matrix on boot, write it to Mongo, then wait ``interval_seconds``
    (default 10 min) and repeat — forever. The wait is AFTER the build (not a fixed wall-clock period), so
    however long a build takes, the next one never piles up: the worker is always either building or sleeping
    the gap. A failed loop is logged and the loop continues (the last-good document keeps serving); it does not
    crash the worker. Designed for a ``restart: unless-stopped`` container."""
    logger.info("store_grid worker starting: store=%s interval=%ss mongo=%s", root, interval_seconds, url)
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
        except PyMongoError as exc:
            logger.error("store_grid: Mongo error this loop, keeping last-good: %s", exc)
        time.sleep(max(1.0, interval_seconds))


def main() -> None:
    """Worker entrypoint: ``python -m store_grid_cache`` (the store-grid-worker container CMD)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
