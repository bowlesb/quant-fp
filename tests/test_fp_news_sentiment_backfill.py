"""The sentiment schema migration: legacy ``/store/news`` partitions written BEFORE the sentiment columns
existed must (a) read back without error (the column is filled null) and (b) be re-scorable in place by the
idempotent backfill script — deterministically, from each row's own stored text, matching the ingest score."""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.data.news_sentiment import MODEL_VERSION, score_article
from quantlib.data.news_sentiment_backfill import rescore_store, score_frame
from quantlib.data.news_store import (
    NEWS_SCHEMA,
    SRC_BACKFILL,
    _read_partition,
    _write_partition_atomic,
    load_news,
)

UTC = dt.timezone.utc

# A column set matching the store schema MINUS the two sentiment columns — a legacy partition's shape.
_LEGACY_SCHEMA = {name: dtype for name, dtype in NEWS_SCHEMA.items() if not name.startswith("sentiment")}


def _legacy_row(article_id: int, available_at: dt.datetime, headline: str, summary: str) -> dict:
    return {
        "id": article_id,
        "symbols": ["AAPL"],
        "available_at": available_at,
        "available_at_source": SRC_BACKFILL,
        "published_at": available_at,
        "updated_at": available_at,
        "headline": headline,
        "summary": summary,
        "source": "Benzinga",
        "author": "author",
        "url": "https://example.com/a",
        "ingested_at": available_at,
    }


def _write_legacy_partition(store: str, published_date: dt.date, rows: list[dict]) -> None:
    """Write a partition with the OLD (pre-sentiment) schema, simulating a store populated before the
    sentiment field was added."""
    frame = pl.DataFrame(rows, schema=_LEGACY_SCHEMA)
    _write_partition_atomic(store, published_date, frame)


def test_read_partition_conforms_legacy_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A legacy partition (no sentiment columns) reads back conformed to the full schema, sentiment null."""
    store = str(tmp_path)
    avail = dt.datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
    _write_legacy_partition(store, avail.date(), [_legacy_row(1, avail, "surge", "rally")])
    frame = _read_partition(store, avail.date())
    assert list(frame.columns) == list(NEWS_SCHEMA.keys())
    assert frame["sentiment"].to_list() == [None]
    assert frame["sentiment_model_version"].to_list() == [None]


def test_load_news_reads_legacy_partition(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The shared loader does not trip on a legacy partition that lacks the sentiment columns."""
    store = str(tmp_path)
    avail = dt.datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
    _write_legacy_partition(store, avail.date(), [_legacy_row(7, avail, "plunge", "crash")])
    loaded = load_news("2026-06-18", "2026-06-18", store=store)
    assert loaded.height == 1
    assert loaded["sentiment"].to_list() == [None]


def test_score_frame_is_deterministic_and_matches_ingest() -> None:
    """score_frame fills a stale row's sentiment with the SAME value the ingest path would stamp."""
    avail = dt.datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
    frame = pl.DataFrame(
        [_legacy_row(1, avail, "beats earnings", "raises guidance, record revenue")], schema=_LEGACY_SCHEMA
    )
    scored, n_stale = score_frame(frame)
    assert n_stale == 1
    assert scored["sentiment"][0] == score_article("beats earnings", "raises guidance, record revenue")
    assert scored["sentiment_model_version"][0] == MODEL_VERSION


def test_rescore_store_dry_run_writes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = str(tmp_path)
    avail = dt.datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
    _write_legacy_partition(
        store,
        avail.date(),
        [_legacy_row(1, avail, "surge", "rally"), _legacy_row(2, avail, "plunge", "fraud")],
    )
    summary = rescore_store(store, apply=False)
    assert summary == {"partitions_scanned": 1, "partitions_touched": 1, "rows_rescored": 2}
    # nothing written: still null on disk
    assert _read_partition(store, avail.date())["sentiment"].to_list() == [None, None]


def test_rescore_store_apply_then_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """--apply fills the scores from stored text; a SECOND apply rewrites nothing (idempotent)."""
    store = str(tmp_path)
    avail = dt.datetime(2026, 6, 18, 14, 30, tzinfo=UTC)
    _write_legacy_partition(
        store,
        avail.date(),
        [_legacy_row(1, avail, "surge rally", ""), _legacy_row(2, avail, "plunge crash", "")],
    )
    first = rescore_store(store, apply=True)
    assert first["rows_rescored"] == 2
    scored = _read_partition(store, avail.date()).sort("id")
    assert scored["sentiment"].to_list() == [1.0, -1.0]  # deterministic from the stored text
    assert scored["sentiment_model_version"].to_list() == [MODEL_VERSION, MODEL_VERSION]
    # second pass: already current → nothing rescored
    second = rescore_store(store, apply=True)
    assert second["rows_rescored"] == 0
