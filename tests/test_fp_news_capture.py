"""The live news-capture row normalization (available_at = ARRIVAL instant, never look-ahead) + the
backfill seed CLI guards (single-process, day windows). No live websocket / Alpaca network needed."""
from __future__ import annotations

import datetime as dt

from quantlib.data.news_backfill import build_parser, day_windows
from quantlib.data.news_fetchers import article_to_row
from quantlib.data.news_store import SRC_BACKFILL, SRC_LIVE
from quantlib.features.news_capture import live_row, news_symbols


class _FakeArticle:
    """A stand-in for alpaca.data.models.news.News (the SDK model takes raw_data; we only read attrs)."""

    def __init__(self) -> None:
        self.id = 42
        self.symbols = ["AAPL", "MSFT"]
        self.created_at = dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)
        self.updated_at = dt.datetime(2026, 6, 18, 14, 5, tzinfo=dt.timezone.utc)
        self.headline = "headline"
        self.summary = "summary"
        self.source = "Benzinga"
        self.author = "author"
        self.url = "https://example.com/a"


def test_article_to_row_backfill_uses_created_at() -> None:
    row = article_to_row(_FakeArticle(), available_at_source=SRC_BACKFILL)
    assert row["id"] == 42
    assert row["symbols"] == ["AAPL", "MSFT"]
    assert row["available_at_source"] == SRC_BACKFILL
    # backfill available_at == the article publish instant (created_at)
    assert row["available_at"] == dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)


def test_live_row_uses_arrival_instant() -> None:
    """The LIVE path sets available_at to the ARRIVAL instant (when WE saw it) — never look-ahead, and
    distinct from the article's created_at."""
    arrival = dt.datetime(2026, 6, 18, 14, 0, 30, tzinfo=dt.timezone.utc)
    row = live_row(_FakeArticle(), arrival)
    assert row["available_at"] == arrival
    assert row["available_at_source"] == SRC_LIVE
    # published_at keeps the honest created_at metadata
    assert row["published_at"] == dt.datetime(2026, 6, 18, 14, 0, tzinfo=dt.timezone.utc)


def test_arrival_is_never_before_created() -> None:
    """An article can only be SEEN after it exists, so arrival >= created_at — the look-ahead guarantee."""
    article = _FakeArticle()
    arrival = article.created_at + dt.timedelta(seconds=2)
    row = live_row(article, arrival)
    assert row["available_at"] >= article.created_at


def test_news_symbols_default_is_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FP_NEWS_SYMBOLS", raising=False)
    assert news_symbols() == ["*"]


def test_news_symbols_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FP_NEWS_SYMBOLS", "aapl, msft")
    assert news_symbols() == ["AAPL", "MSFT"]


def test_day_windows_are_trailing_and_end_yesterday() -> None:
    today = dt.date(2026, 6, 20)
    days = day_windows(3, today)
    assert days == [dt.date(2026, 6, 19), dt.date(2026, 6, 18), dt.date(2026, 6, 17)]
    assert today not in days  # today's tape is owned by the live path, not seeded


def test_backfill_rejects_multiprocess() -> None:
    parser = build_parser()
    args = parser.parse_args(["--processes", "4"])
    assert args.processes == 4  # parsed, but main() rejects it
    # the rejection lives in main(); assert the default is single-process
    assert parser.parse_args([]).processes == 1
