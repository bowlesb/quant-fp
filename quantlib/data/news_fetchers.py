"""Historical RAW news fetcher for the shared ``/store/news/`` dataset (Alpaca v1beta1/news).

Full-fidelity, paginated fetch of news articles from Alpaca's News API for a symbol list over a UTC date
window, normalized into the ``news_store.NEWS_SCHEMA`` shape. Pagination is handled via the SDK's
``next_page_token`` (page size capped at 50, the Alpaca max). A thin retry/back-off wrapper keeps a
transient 429/5xx from aborting the seed.

This is the BACKFILL side: ``available_at`` is set to Alpaca's ``created_at`` (the article publish
instant), flagged ``available_at_source = SRC_BACKFILL``. The LIVE side (``news_capture``) sets
``available_at`` to the websocket arrival instant instead — see ``news_store`` parity contract.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from alpaca.common.exceptions import APIError
from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest

from quantlib.data.news_sentiment import MODEL_VERSION, score_article
from quantlib.data.news_store import SRC_BACKFILL

logger = logging.getLogger("news_fetchers")

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
ALPACA_NEWS_PAGE_LIMIT = 50  # Alpaca v1beta1/news max page size.


def build_news_client(key_id: str, secret_key: str) -> NewsClient:
    """The historical news REST client (v1beta1/news). Same Alpaca paper keys as the market-data tape."""
    return NewsClient(api_key=key_id, secret_key=secret_key)


def article_to_row(article: News, available_at_source: str = SRC_BACKFILL) -> dict:
    """Normalize an Alpaca ``News`` model into a ``news_store.NEWS_SCHEMA`` row.

    ``available_at`` defaults to the article's ``created_at`` (publish instant) for the backfill path;
    the live path overrides ``available_at`` to the websocket arrival instant before persisting. All
    datetimes are coerced to tz-aware UTC.

    The baseline ``sentiment`` is scored here from ``headline`` + ``summary`` ONLY (the single normalization
    point both the live capture and the historical backfill funnel through), so every stored article carries
    a deterministic, parity-stable score stamped at first sight — identical live vs backfill because the text
    is identical on both sides.
    """
    created = _as_utc(article.created_at)
    return {
        "id": int(article.id),
        "symbols": list(article.symbols),
        "available_at": created,
        "available_at_source": available_at_source,
        "published_at": created,
        "updated_at": _as_utc(article.updated_at),
        "headline": article.headline,
        "summary": article.summary,
        "source": article.source,
        "author": article.author,
        "url": article.url or "",
        "ingested_at": dt.datetime.now(dt.timezone.utc),
        "sentiment": score_article(article.headline, article.summary),
        "sentiment_model_version": MODEL_VERSION,
    }


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _retry_get(client: NewsClient, request: NewsRequest):  # type: ignore[no-untyped-def]
    """Issue one ``get_news`` call with bounded exponential back-off on transient API errors."""
    attempt = 0
    while True:
        try:
            return client.get_news(request)
        except APIError as exc:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise
            sleep_s = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
            logger.warning(
                "Alpaca news API error (attempt %d/%d): %s — backing off %.1fs",
                attempt,
                MAX_RETRIES,
                exc,
                sleep_s,
            )
            time.sleep(sleep_s)


def fetch_news_window(
    client: NewsClient,
    symbols: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict]:
    """Fetch every article mentioning any of ``symbols`` over ``[start, end]`` (UTC), fully paginated.

    Returns a list of ``NEWS_SCHEMA`` rows (one per article) with ``available_at = created_at`` flagged
    backfill. ``include_content`` is False (we store headline + summary, not the full HTML body — the
    hotness hunt is count/intensity, not content parsing). De-dup by ``id`` is the store's job
    (first-sight wins), so this returns the raw paginated stream.
    """
    rows: list[dict] = []
    page_token: str | None = None
    while True:
        request = NewsRequest(
            symbols=",".join(symbols),
            start=start,
            end=end,
            limit=ALPACA_NEWS_PAGE_LIMIT,
            include_content=False,
            sort="asc",
            page_token=page_token,
        )
        news_set = _retry_get(client, request)
        articles = news_set.data.get("news", [])
        for article in articles:
            rows.append(article_to_row(article, available_at_source=SRC_BACKFILL))
        page_token = news_set.next_page_token
        if not page_token:
            break
    return rows
