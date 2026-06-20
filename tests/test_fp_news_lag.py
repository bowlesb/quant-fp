"""The live news feed-delay LAG measurement (available_at − published_at over live-arrival rows), whose
p90 calibrates the hotness hunt's EMBARGO. Pins: live-only isolation, lag arithmetic, the
insufficient-sample gate, and the summary quantiles. No live websocket, no Alpaca network."""

from __future__ import annotations

import datetime as dt

from quantlib.data.news_lag import MIN_LIVE_SAMPLE, lag_summary, load_live_lag_seconds, report
from quantlib.data.news_store import SRC_BACKFILL, SRC_LIVE, upsert_articles


def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)


def _row(
    article_id: int,
    available_at: dt.datetime,
    published_at: dt.datetime,
    source: str,
) -> dict:
    return {
        "id": article_id,
        "symbols": ["AAPL"],
        "available_at": available_at,
        "available_at_source": source,
        "published_at": published_at,
        "updated_at": published_at,
        "headline": "headline",
        "summary": "summary",
        "source": "Benzinga",
        "author": "author",
        "url": "https://example.com/a",
        "ingested_at": available_at,
    }


def test_lag_is_arrival_minus_published_for_live_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = str(tmp_path)
    published = _utc(2026, 6, 18, 14, 30, 0)
    arrived = _utc(2026, 6, 18, 14, 30, 45)  # 45s feed delay
    upsert_articles(store, [_row(1, arrived, published, SRC_LIVE)], SRC_LIVE)
    lag = load_live_lag_seconds(store)
    assert lag.height == 1
    assert abs(lag["lag_seconds"][0] - 45.0) < 1e-6


def test_backfill_rows_excluded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A backfill row (available_at == published_at == created_at) is NOT a real feed-delay sample."""
    store = str(tmp_path)
    created = _utc(2026, 6, 18, 14, 30, 0)
    arrived = _utc(2026, 6, 18, 14, 31, 0)
    upsert_articles(store, [_row(1, created, created, SRC_BACKFILL)], SRC_BACKFILL)
    upsert_articles(store, [_row(2, arrived, created, SRC_LIVE)], SRC_LIVE)
    lag = load_live_lag_seconds(store)
    assert lag.height == 1  # only the live row
    assert abs(lag["lag_seconds"][0] - 60.0) < 1e-6


def test_empty_store_returns_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lag = load_live_lag_seconds(str(tmp_path))
    assert lag.height == 0


def test_summary_quantiles(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = str(tmp_path)
    published = _utc(2026, 6, 18, 14, 0, 0)
    rows = [_row(i, published + dt.timedelta(seconds=i), published, SRC_LIVE) for i in range(1, 101)]
    upsert_articles(store, rows, SRC_LIVE)
    summary = lag_summary(load_live_lag_seconds(store))
    assert summary["count"] == 100
    # nearest-rank quantiles over 1..100s: monotone, p50<p90<p99, bounded by min/max.
    assert summary["min_seconds"] == 1.0
    assert summary["max_seconds"] == 100.0
    assert 49.0 <= summary["p50_seconds"] <= 51.0
    assert 89.0 <= summary["p90_seconds"] <= 91.0
    assert summary["p50_seconds"] < summary["p90_seconds"] < summary["p99_seconds"]


def test_insufficient_sample_gate(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    store = str(tmp_path)
    published = _utc(2026, 6, 18, 14, 0, 0)
    rows = [_row(i, published + dt.timedelta(seconds=i), published, SRC_LIVE) for i in range(5)]
    upsert_articles(store, rows, SRC_LIVE)
    with caplog.at_level("INFO"):
        report(store)
    assert "INSUFFICIENT live overlap" in caplog.text
    assert str(MIN_LIVE_SAMPLE) in caplog.text
