"""Always-warm MongoDB cache + background WORKER for the FLEET under-represented-ticker report.

``ops/underrepresented_tickers.py`` (#464) ranks, across the WHOLE universe, the symbols the feature-computer
is settled-in-backfill-but-not-streaming (the FP_TICK_SYMBOLS gap). That is a ~7000-symbol full-store walk —
correctly kept OFF the dashboard request path. This module is the background worker that runs that walk on a
SLOW cadence and writes the last-good report to MongoDB, plus the read side the dashboard route serves.

WHY a worker (not a request-path build): a full-universe walk on every page load would be a latency/DoS
footgun, and on a loaded box a frequent walk would contend with the live pipeline. So:

  * WRITE side (``run_forever`` / ``write_report``) — the worker container's permanent loop. It runs ONE
    bounded walk (the same bounded reads as #464: a few recent backfill dates + the recent stream window, a
    <=12-file sample per partition), writes the gzipped report to Mongo, then SLEEPS a long interval (default
    DAILY). The wait is AFTER the build so a slow build never piles up. A failed loop is logged and the loop
    continues (the last-good document keeps serving) — designed for a ``restart: unless-stopped`` container.
    An optional start delay keeps the first build off a peak window.

  * READ side (``read_report`` / ``read_meta``) — the dashboard route. A single indexed Mongo lookup, served
    from the last-good document, so a page refresh is O(1) and never triggers the walk. ``None`` on a cold
    cache (first-ever boot) or unreachable Mongo → the route reports ``booting`` (the store-grid convention),
    never a recurring "warming".

MongoDB is the dedicated ``mongo`` compose service the store-grid worker already uses (NOT the feature
pipeline's store). The store is mounted READ-ONLY in the worker. ENGINE-FREE: this report needs no trust/DB
and no feature engine — it imports only the engine-free ``underrepresented_tickers`` reader. NO feature-store
schema/format/fingerprint change — this is a read-side cache only.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from underrepresented_tickers import (
    DEFAULT_BACKFILL_DATES,
    DEFAULT_STREAM_WINDOW_DAYS,
    PartitionStoreReader,
    build_report,
)

logger = logging.getLogger("underrep")

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# The dedicated cache Mongo (the ``mongo`` compose service on quant_default) — the same one store-grid uses.
# Overridable for tests / a move.
UNDERREP_MONGO_URL = os.environ.get("UNDERREP_MONGO_URL", "mongodb://quant-mongo:27017")
UNDERREP_DB_NAME = os.environ.get("UNDERREP_MONGO_DB", "dashboard")

# One doc for the gzipped report, one for the small meta header. A schema tag in the collection name lets a
# future payload-shape change roll cleanly past a stale document.
REPORT_COLLECTION = "underrep_report"
META_COLLECTION = "underrep_meta"
REPORT_DOC_ID = "report"
META_DOC_ID = "meta"

# SLOW cadence by design — the box is loaded and the integration is the priority, so this is a DAILY refresh,
# not a hot loop. The under-rep gap moves only as the stream tier or backfill coverage changes (days, not
# minutes). The wait is AFTER the build so a slow build never piles up.
INTERVAL_SECONDS = int(os.environ.get("UNDERREP_INTERVAL_SECONDS", str(24 * 60 * 60)))

# Optional one-time delay before the FIRST build, so a container (re)start during a peak/contended window does
# not immediately fire the full-universe walk. Default 0 (build on boot); set e.g. to push the first walk to a
# quiet hour. Bounded reads keep even a peak-time build light, but this is a cheap extra guard.
STARTUP_DELAY_SECONDS = int(os.environ.get("UNDERREP_STARTUP_DELAY_SECONDS", "0"))

_CONNECT_TIMEOUT_MS = 3000
_SERVER_SELECT_TIMEOUT_MS = 3000


def _client(url: str = UNDERREP_MONGO_URL) -> MongoClient:
    """A short-timeout Mongo client so the read path fails fast when Mongo is down."""
    return MongoClient(
        url,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        serverSelectionTimeoutMS=_SERVER_SELECT_TIMEOUT_MS,
    )


def write_report(
    root: str = STORE_ROOT,
    backfill_dates: int = DEFAULT_BACKFILL_DATES,
    stream_days: int = DEFAULT_STREAM_WINDOW_DAYS,
    url: str = UNDERREP_MONGO_URL,
) -> dict[str, object]:
    """Run the bounded under-rep walk ONCE and UPSERT the gzipped report + a small meta doc into Mongo. Returns
    a small summary for the worker to log. Lets Mongo / build errors RAISE so a failed loop is loud in the
    worker log rather than silently writing nothing."""
    client = _client(url)
    database = client[UNDERREP_DB_NAME]
    started = time.monotonic()

    report = build_report(PartitionStoreReader(root), backfill_dates=backfill_dates, stream_days=stream_days)
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw = json.dumps(report).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=6)

    database[REPORT_COLLECTION].replace_one(
        {"_id": REPORT_DOC_ID},
        {"_id": REPORT_DOC_ID, "gzip": blob, "generated_at": report["generated_at"]},
        upsert=True,
    )

    summary = {
        "generated_at": report["generated_at"],
        "n_symbols_seen": report["n_symbols_seen"],
        "n_symbols_backfilled": report["n_symbols_backfilled"],
        "n_symbols_streamed": report["n_symbols_streamed"],
        "n_symbols_under_represented": report["n_symbols_under_represented"],
        "backfill_dates_sampled": report["backfill_dates_sampled"],
        "stream_window_days": report["stream_window_days"],
        "raw_bytes": len(raw),
        "gzip_bytes": len(blob),
        "build_seconds": round(time.monotonic() - started, 1),
    }
    database[META_COLLECTION].replace_one({"_id": META_DOC_ID}, {"_id": META_DOC_ID, **summary}, upsert=True)
    return summary


def read_report(url: str = UNDERREP_MONGO_URL) -> dict[str, object] | None:
    """The decompressed under-rep report exactly as the worker wrote it. ``None`` on a cold cache (first-ever
    boot, no document yet) or unreachable Mongo — the route reports booting."""
    try:
        client = _client(url)
        doc = client[UNDERREP_DB_NAME][REPORT_COLLECTION].find_one({"_id": REPORT_DOC_ID})
    except PyMongoError:
        logger.warning("underrep: Mongo unreachable on report read")
        return None
    if doc is None:
        return None
    result: dict[str, object] = json.loads(gzip.decompress(bytes(doc["gzip"])).decode("utf-8"))
    return result


def read_meta(url: str = UNDERREP_MONGO_URL) -> dict[str, object] | None:
    """The small meta header (generated_at, counts) for the UI's "as of" staleness. ``None`` if not built yet.
    The Mongo ``_id`` is stripped so the payload is exactly the summary the worker wrote."""
    try:
        client = _client(url)
        doc = client[UNDERREP_DB_NAME][META_COLLECTION].find_one({"_id": META_DOC_ID})
    except PyMongoError:
        logger.warning("underrep: Mongo unreachable on meta read")
        return None
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


def run_forever(
    root: str = STORE_ROOT,
    interval_seconds: int = INTERVAL_SECONDS,
    startup_delay_seconds: int = STARTUP_DELAY_SECONDS,
    url: str = UNDERREP_MONGO_URL,
) -> None:
    """The worker's permanent loop: optionally wait an off-peak startup delay, then build the report, write it
    to Mongo, wait ``interval_seconds`` (default DAILY), and repeat — forever. The wait is AFTER the build so a
    slow build never piles up. A failed loop is logged and the loop continues (the last-good document keeps
    serving); it does not crash the worker. Designed for a ``restart: unless-stopped`` container."""
    logger.info(
        "underrep worker starting: store=%s interval=%ss startup_delay=%ss mongo=%s",
        root,
        interval_seconds,
        startup_delay_seconds,
        url,
    )
    if startup_delay_seconds > 0:
        logger.info("underrep: holding %ss before the first build (off-peak guard)", startup_delay_seconds)
        time.sleep(startup_delay_seconds)
    while True:
        try:
            summary = write_report(root=root, url=url)
            logger.info(
                "underrep: wrote report — %s under-represented of %s seen (%s backfilled / %s streamed) "
                "gzip=%.1fKB in %.1fs",
                summary["n_symbols_under_represented"],
                summary["n_symbols_seen"],
                summary["n_symbols_backfilled"],
                summary["n_symbols_streamed"],
                int(summary["gzip_bytes"]) / 1024,  # type: ignore[call-overload]
                summary["build_seconds"],
            )
        except PyMongoError as exc:
            logger.error("underrep: Mongo error this loop, keeping last-good: %s", exc)
        time.sleep(max(1.0, interval_seconds))


def main() -> None:
    """Worker entrypoint: ``python -m underrep_cache`` (the underrep-worker container CMD)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
