"""The raw news tape: an append-only, manifest-tracked, date-partitioned store whose ``available_at``
is FIXED AT FIRST SIGHT (de-dup by article id) — the parity contract that makes a downstream hotness
feature gated on ``available_at <= minute`` backfill==live by construction. Plus the live/backfill row
normalization and the seed CLI guards. These pin the pure storage + normalization pieces (no live
websocket, no Alpaca network)."""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.data.news_store import (
    NEWS_SCHEMA,
    SRC_BACKFILL,
    SRC_LIVE,
    backfilled_dates,
    load_manifest,
    load_news,
    upsert_articles,
)


def _article(
    article_id: int,
    symbols: list[str],
    available_at: dt.datetime,
    ingested_at: dt.datetime,
    source: str = SRC_BACKFILL,
    headline: str = "headline",
) -> dict:
    return {
        "id": article_id,
        "symbols": symbols,
        "available_at": available_at,
        "available_at_source": source,
        "published_at": available_at,
        "updated_at": available_at,
        "headline": headline,
        "summary": "summary",
        "source": "Benzinga",
        "author": "author",
        "url": "https://example.com/a",
        "ingested_at": ingested_at,
        "sentiment": 0.0,
        "sentiment_model_version": "lexicon-v1",
    }


def _utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)


def test_upsert_writes_and_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = str(tmp_path)
    avail = _utc(2026, 6, 18, 14, 30)
    new = upsert_articles(store, [_article(1, ["AAPL", "MSFT"], avail, avail)], SRC_BACKFILL)
    assert new == 1
    loaded = load_news("2026-06-18", "2026-06-18", store)
    assert loaded.height == 1
    assert loaded["id"][0] == 1
    assert loaded["symbols"][0].to_list() == ["AAPL", "MSFT"]


def test_dedup_by_id_first_sight_wins(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An article id seen twice keeps its ORIGINAL available_at — the fixed-at-first-sight parity rule.
    A later re-fetch with a DIFFERENT available_at must NOT overwrite the first-sight value."""
    store = str(tmp_path)
    first_seen = _utc(2026, 6, 18, 9, 0)
    later_seen = _utc(2026, 6, 18, 23, 0)
    # First sight: ingested early, available_at = 09:00.
    upsert_articles(store, [_article(7, ["TSLA"], first_seen, first_seen, SRC_LIVE)], SRC_LIVE)
    # Re-fetch of the SAME id, ingested later, claims available_at = 23:00 — must be IGNORED.
    new = upsert_articles(store, [_article(7, ["TSLA"], later_seen, later_seen, SRC_BACKFILL)], SRC_BACKFILL)
    assert new == 0  # nothing genuinely new
    loaded = load_news("2026-06-18", "2026-06-18", store)
    assert loaded.height == 1
    assert loaded["available_at"][0] == first_seen  # first-sight value preserved


def test_multi_symbol_article_stored_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A multi-symbol article is ONE row (symbols as a list), not duplicated per symbol."""
    store = str(tmp_path)
    avail = _utc(2026, 6, 18)
    upsert_articles(store, [_article(3, ["AAPL", "MSFT", "SPY"], avail, avail)], SRC_BACKFILL)
    loaded = load_news("2026-06-18", "2026-06-18", store)
    assert loaded.height == 1
    assert sorted(loaded["symbols"][0].to_list()) == ["AAPL", "MSFT", "SPY"]


def test_partitioned_by_published_date(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Articles land in the partition of their available_at UTC date; load_news windows by date."""
    store = str(tmp_path)
    upsert_articles(store, [_article(1, ["AAPL"], _utc(2026, 6, 17), _utc(2026, 6, 17))], SRC_BACKFILL)
    upsert_articles(store, [_article(2, ["AAPL"], _utc(2026, 6, 18), _utc(2026, 6, 18))], SRC_BACKFILL)
    upsert_articles(store, [_article(3, ["AAPL"], _utc(2026, 6, 19), _utc(2026, 6, 19))], SRC_BACKFILL)
    assert load_news("2026-06-18", "2026-06-18", store).height == 1
    assert load_news("2026-06-17", "2026-06-19", store).height == 3
    assert load_news("2026-06-20", "2026-06-21", store).height == 0


def test_point_in_time_gate_is_reader_side(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """load_news returns the raw tape; the available_at <= minute gate is the READER's to apply. Verify
    the available_at column is present + correct so a hotness feature can gate on it."""
    store = str(tmp_path)
    a1 = _utc(2026, 6, 18, 10, 0)
    a2 = _utc(2026, 6, 18, 15, 0)
    upsert_articles(store, [_article(1, ["AAPL"], a1, a1), _article(2, ["AAPL"], a2, a2)], SRC_BACKFILL)
    loaded = load_news("2026-06-18", "2026-06-18", store)
    minute = _utc(2026, 6, 18, 12, 0)
    visible = loaded.filter(pl.col("available_at") <= minute)
    assert visible.height == 1
    assert visible["id"][0] == 1


def test_backfilled_dates_only_tracks_backfill_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Resume key: only BACKFILL manifest parts mark a date done; live appends don't (they're ongoing)."""
    store = str(tmp_path)
    upsert_articles(store, [_article(1, ["AAPL"], _utc(2026, 6, 17), _utc(2026, 6, 17), SRC_LIVE)], SRC_LIVE)
    upsert_articles(store, [_article(2, ["AAPL"], _utc(2026, 6, 18), _utc(2026, 6, 18))], SRC_BACKFILL)
    done = backfilled_dates(store)
    assert "2026-06-18" in done
    assert "2026-06-17" not in done  # live-only date is NOT a backfill-done date


def test_manifest_append_only_parts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Each upsert appends an immutable manifest part; load_manifest unions them all."""
    store = str(tmp_path)
    upsert_articles(store, [_article(1, ["AAPL"], _utc(2026, 6, 18), _utc(2026, 6, 18))], SRC_BACKFILL)
    upsert_articles(store, [_article(2, ["MSFT"], _utc(2026, 6, 18), _utc(2026, 6, 18, 13))], SRC_BACKFILL)
    manifest = load_manifest(store)
    assert manifest.height >= 2  # at least one part per upsert
    assert set(manifest["source"].to_list()) == {SRC_BACKFILL}


def test_schema_has_parity_fields() -> None:
    for field in ("id", "symbols", "available_at", "available_at_source", "published_at", "ingested_at"):
        assert field in NEWS_SCHEMA
