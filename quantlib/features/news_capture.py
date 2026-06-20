"""24/7 LIVE news capture — an ISOLATED ingestion service that streams Alpaca's news websocket
(v1beta1/news) into the append-only, manifest-tracked ``/store/news/`` tape. Mirrors the
``crypto_capture`` pattern (a detached, restartable, idempotent, 24/7-capable container that runs the
SAME storage path as the backfill seeder), but this is a RAW ACQUISITION service — it computes NO
features and NEVER touches the equity feature-computer, its fingerprint, its store, or its bus.

Design:

* **Feed** — ``alpaca.data.live.NewsDataStream`` (endpoint ``v1beta1/news``). News runs continuously
  (24/7, not market-hours gated), same Alpaca paper keys as the equity tape (read from env).
* **Subscription** — ``"*"`` (all symbols) by default, so we capture every article and the hotness hunt
  can pick its own universe at read time; ``FP_NEWS_SYMBOLS`` overrides with a comma list.
* **Availability (the parity contract)** — each streamed article's ``available_at`` is set to the
  ARRIVAL instant (when WE saw it on the socket), flagged ``available_at_source = SRC_LIVE``. This is
  never look-ahead (we can only see an article after it exists). Because the store de-dups by article
  ``id`` first-sight-wins, an id is written exactly ONCE and its ``available_at`` is then immutable — so
  a (symbol, minute) hotness feature gated on ``available_at <= minute`` is parity-stable.
* **Idempotency / restart-safety** — articles are flushed to the store in micro-batches; the store
  upsert de-dups by id, so a restart (or an overlap between this live feed and the historical backfill)
  never double-counts. The websocket auto-reconnects (the SDK handles it); a crash + container restart
  simply resumes streaming, with at-most one in-flight micro-batch lost (re-seen on the next backfill).

Run inside fp-dev (a SEPARATE ``news-capture`` container — NOT the equity feature-computer)::

    docker compose -f docker-compose.news.yml up -d news-capture

This is a RAW DATA ACQUISITION service. Features off this tape (news hotness) come LATER via the
Modeller's pre-registered hunt as a separate, fingerprint-affecting PR — NOT here.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time

from alpaca.data.live.news import NewsDataStream
from alpaca.data.models.news import News

from quantlib.data.news_fetchers import article_to_row
from quantlib.data.news_store import SRC_LIVE, upsert_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("news_capture")

# Flush a micro-batch when it reaches this many articles OR this many seconds since the last flush,
# whichever first — keeps the store partition fresh without a per-article parquet rewrite.
FLUSH_EVERY_ARTICLES = 25
FLUSH_EVERY_SECONDS = 30.0


def news_symbols() -> list[str]:
    """The symbols to subscribe (default ``["*"]`` = all; ``FP_NEWS_SYMBOLS`` overrides, comma list)."""
    env = os.environ.get("FP_NEWS_SYMBOLS", "").strip()
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return ["*"]


def build_stream() -> NewsDataStream:
    """The 24/7 news websocket (v1beta1/news). Same Alpaca paper keys as the equity tape (from env)."""
    return NewsDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        url_override=os.environ.get("NEWS_STREAM_URL_OVERRIDE"),
    )


def live_row(article: News, arrival: dt.datetime) -> dict:
    """A live ``NEWS_SCHEMA`` row: ``available_at`` is the ARRIVAL instant (never look-ahead), flagged
    ``SRC_LIVE``. ``published_at`` keeps Alpaca's ``created_at`` as honest metadata."""
    row = article_to_row(article, available_at_source=SRC_LIVE)
    row["available_at"] = arrival
    return row


def run_news_capture(symbols: list[str], store: str) -> None:  # pragma: no cover (live websocket loop)
    """Own the news websocket, buffer arriving articles, and flush micro-batches into the ``/store/news``
    tape (de-dup by id, parity-safe). Single process — the news firehose is light vs the equity bar feed.
    """
    pending: list[dict] = []
    last_flush = time.time()

    def flush() -> None:
        nonlocal last_flush
        if not pending:
            return
        new_count = upsert_articles(store, list(pending), source=SRC_LIVE)
        logger.info("flushed %d articles (%d new) -> %s/news", len(pending), new_count, store)
        pending.clear()
        last_flush = time.time()

    async def on_news(article: News) -> None:  # type: ignore[no-untyped-def]
        arrival = dt.datetime.now(dt.timezone.utc)
        pending.append(live_row(article, arrival))
        if len(pending) >= FLUSH_EVERY_ARTICLES or (time.time() - last_flush) >= FLUSH_EVERY_SECONDS:
            flush()

    stream = build_stream()
    stream.subscribe_news(on_news, *symbols)
    logger.info(
        "news capture starting: symbols=%s store=%s feed=v1beta1/news (24/7)",
        symbols,
        store,
    )
    stream.run()


def main() -> None:
    store = sys.argv[1] if len(sys.argv) > 1 else "/store"
    symbols = news_symbols()
    logger.info(
        "[news_capture] subscribe=%s -> store=%s (UTC now=%s)",
        symbols,
        store,
        dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    run_news_capture(symbols, store=store)


if __name__ == "__main__":
    main()
