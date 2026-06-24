"""Tests for the dashboard News & Filings module + routes (services/dashboard/news_edgar.py).

Covers, with NO real DB / store / Mongo (every data-access helper is monkeypatched):
  * ``_status_for`` flattens ``data_freshness.grade_age`` into the UI shape and grades business-hours-aware.
  * ``_news_composition`` returns an empty-but-valid payload when no partitions exist.
  * ``composition_snapshot`` caches the heavy build (second call within the TTL is served from cache).
  * the ``/api/news-edgar/stream`` and ``/api/news-edgar/composition`` routes return 200 with the panel shape,
    and a DB error on a side degrades to a partial ``error`` block rather than failing the whole tab.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import news_edgar as ne  # noqa: E402  (path inserted above)

# A Friday mid-morning ET instant (14:00Z = 10:00 ET) — squarely inside SEC business hours, so a fresh source
# grades OK and a stale one grades WARN/STALE rather than the off-hours INACTIVE.
_BUSINESS_MOMENT = datetime(2026, 6, 19, 14, 0, tzinfo=timezone.utc)


def test_status_for_fresh_is_ok_in_business_hours() -> None:
    newest = datetime(2026, 6, 19, 13, 50, tzinfo=timezone.utc)  # 10 min old
    status = ne._status_for(newest, ne.NEWS_WARN_MIN, ne.NEWS_FAIL_MIN, _BUSINESS_MOMENT, "news")
    assert status["status"] == "OK"
    assert status["in_business_hours"] is True
    assert status["age_minutes"] == 10.0


def test_status_for_stale_is_graded_in_business_hours() -> None:
    newest = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc)  # 360 min old, past the EDGAR fail threshold
    status = ne._status_for(newest, ne.EDGAR_WARN_MIN, ne.EDGAR_FAIL_MIN, _BUSINESS_MOMENT, "edgar")
    assert status["status"] == "STALE"


def test_status_for_offhours_is_inactive() -> None:
    # A Saturday instant — outside SEC business hours, so any age is the expected lull, never a failure.
    saturday = datetime(2026, 6, 20, 14, 0, tzinfo=timezone.utc)
    newest = datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc)
    status = ne._status_for(newest, ne.EDGAR_WARN_MIN, ne.EDGAR_FAIL_MIN, saturday, "edgar")
    assert status["status"] == "INACTIVE"
    assert status["in_business_hours"] is False


def test_news_composition_empty_when_no_partitions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ne, "NEWS_GLOB", "/nonexistent/news/published_date=*/data.parquet")
    payload = ne._news_composition()
    assert payload["total_articles"] == 0
    assert payload["n_symbols"] == 0
    assert payload["top_symbols"] == []
    assert payload["earliest_date"] is None


def _write_news_partition(
    directory: Path, published_date: str, when: datetime, symbol: str, sentiment: float | None
) -> str:
    """Write one ``published_date=.../data.parquet`` partition; include ``sentiment`` only when given.

    The re-scored newer partitions carry a ``sentiment`` column the older ones lack, so a multi-file scan
    sees a heterogeneous schema — exactly the condition that 500'd the stream/composition routes.
    """
    part_dir = directory / f"published_date={published_date}"
    part_dir.mkdir(parents=True, exist_ok=True)
    columns: dict[str, object] = {
        "available_at_source": [ne.SRC_LIVE],
        "available_at": [when],
        "symbols": [[symbol]],
    }
    if sentiment is not None:
        columns["sentiment"] = [sentiment]
    path = part_dir / "data.parquet"
    pl.DataFrame(columns).write_parquet(path)
    return str(path)


def test_news_stream_tolerates_extra_sentiment_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A newer partition carrying an extra ``sentiment`` column must not raise a SchemaError on scan."""
    reference = _BUSINESS_MOMENT
    old_path = _write_news_partition(
        tmp_path, "2026-06-18", reference - timedelta(minutes=30), "AAPL", sentiment=None
    )
    new_path = _write_news_partition(
        tmp_path, "2026-06-19", reference - timedelta(minutes=10), "MSFT", sentiment=0.42
    )
    monkeypatch.setattr(ne, "recent_news_partition_paths", lambda limit: [old_path, new_path])
    payload = ne._news_stream(reference)
    # Both live rows fall inside the trailing rate window — the scan must read across both schemas.
    assert payload["window_count"] == 2
    assert payload["per_min"] == round(2 / ne.RATE_WINDOW_MIN, 3)


def test_news_composition_tolerates_extra_sentiment_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full-history composition glob spans schemas with/without ``sentiment``; it must not raise."""
    base = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)
    _write_news_partition(tmp_path, "2026-06-18", base, "AAPL", sentiment=None)
    _write_news_partition(tmp_path, "2026-06-19", base, "MSFT", sentiment=0.42)
    monkeypatch.setattr(ne, "NEWS_GLOB", str(tmp_path / "published_date=*/data.parquet"))
    payload = ne._news_composition()
    assert payload["total_articles"] == 2
    assert payload["n_symbols"] == 2
    assert payload["earliest_date"] == "2026-06-18"
    assert payload["latest_date"] == "2026-06-19"


def _stub_build(monkeypatch: pytest.MonkeyPatch, marker: list[int]) -> None:
    """Replace the heavy composition build with a counter so cache hits are observable."""

    def fake_build() -> dict[str, object]:
        marker[0] += 1
        return {"generated_at": "t", "news": {}, "filings": {}, "features": []}

    monkeypatch.setattr(ne, "_build_composition", fake_build)


def test_composition_snapshot_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ne, "_composition_cache", None)
    calls = [0]
    _stub_build(monkeypatch, calls)
    first = ne.composition_snapshot()
    second = ne.composition_snapshot()
    assert calls[0] == 1  # built once, second call served from cache
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["cache_age_seconds"] >= 0.0


class _FakeFilingsCursor:
    """A cursor over an in-memory filings table that honours the ``available_at`` window bounds.

    The span queries (``ORDER BY available_at ASC/DESC LIMIT 1``) return the min/max instant; the windowed
    ``GROUP BY form_type`` query returns only the rows whose ``available_at`` falls in the ``[start, end)``
    bounds it is passed — so a test sees the SAME chunk-exclusion the real bounded query gets, and proves the
    Python merge reassembles the whole-store totals from the per-window partials.
    """

    def __init__(self, rows: list[tuple[datetime, str, str]]) -> None:
        # rows: (available_at, form_type, source)
        self._rows = rows
        self._result: list[tuple[object, ...]] = []

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        if "ORDER BY available_at ASC" in query:
            self._result = [(min(row[0] for row in self._rows),)] if self._rows else []
        elif "ORDER BY available_at DESC" in query:
            self._result = [(max(row[0] for row in self._rows),)] if self._rows else []
        else:
            assert params is not None
            start, end = params
            counts: dict[str, list[int]] = {}
            for when, form_type, source in self._rows:
                if start <= when < end:
                    bucket = counts.setdefault(form_type, [0, 0])
                    bucket[0] += 1
                    if source == "stream":
                        bucket[1] += 1
            self._result = [(form_type, n, sn) for form_type, (n, sn) in counts.items()]

    def fetchone(self) -> tuple[object, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._result

    def __enter__(self) -> "_FakeFilingsCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeFilingsConn:
    def __init__(self, rows: list[tuple[datetime, str, str]]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeFilingsCursor:
        return _FakeFilingsCursor(self._rows)


def test_filings_composition_empty_store() -> None:
    payload = ne._filings_composition(_FakeFilingsConn([]))
    assert payload["total_filings"] == 0
    assert payload["stream_filings"] == 0
    assert payload["earliest_available_at"] is None
    assert payload["latest_available_at"] is None
    assert payload["form_types"] == []


def test_filings_composition_windows_merge_to_whole_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows spanning more than one window must merge into the whole-store totals + a full form-type breakdown.

    With a small window, the span (~400 days here) forces several bounded queries; the merged result must
    equal a single full-table aggregate, proving the chunk-bounded walk loses no rows at the window seams.
    """
    monkeypatch.setattr(ne, "FILINGS_WINDOW_DAYS", 100)
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = [
        (base + timedelta(days=0), "4", "backfill"),
        (base + timedelta(days=50), "8-K", "backfill"),
        (base + timedelta(days=150), "4", "stream"),  # 2nd window
        (base + timedelta(days=250), "10-Q", "backfill"),  # 3rd window
        (base + timedelta(days=399), "4", "stream"),  # last window
    ]
    payload = ne._filings_composition(_FakeFilingsConn(rows))
    assert payload["total_filings"] == 5
    assert payload["stream_filings"] == 2
    assert payload["earliest_available_at"] == rows[0][0].isoformat()
    assert payload["latest_available_at"] == rows[-1][0].isoformat()
    # form_type "4" appears in three separate windows and must sum across them, ranked first.
    by_type = {entry["form_type"]: entry["count"] for entry in payload["form_types"]}
    assert by_type == {"4": 3, "8-K": 1, "10-Q": 1}
    assert payload["form_types"][0]["form_type"] == "4"


def _import_app() -> object:
    import app as dashboard_app  # noqa: E402  (path inserted above)

    return dashboard_app


def test_stream_route_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_stream() -> dict[str, object]:
        return {
            "generated_at": "2026-06-19T14:00:00+00:00",
            "news": {
                "per_min": 1.5,
                "window_count": 90,
                "window_minutes": 60,
                "timeline": [],
                "freshness": {},
            },
            "edgar": {
                "per_min": 0.0,
                "window_count": 0,
                "window_minutes": 60,
                "timeline": [],
                "freshness": {},
            },
        }

    dashboard_app = _import_app()
    monkeypatch.setattr(dashboard_app, "stream_snapshot", fake_stream)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/news-edgar/stream")
    assert resp.status_code == 200
    body = resp.json()
    assert body["news"]["per_min"] == 1.5
    assert body["edgar"]["window_count"] == 0


def test_composition_route_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_comp() -> dict[str, object]:
        return {
            "generated_at": "2026-06-19T14:00:00+00:00",
            "news": {"total_articles": 27271, "n_symbols": 6377, "top_symbols": []},
            "filings": {"total_filings": 3175782, "form_types": []},
            "features": [{"label": "edgar_filing_frequency", "status": "LIVE"}],
            "cached": False,
            "cache_age_seconds": 0.0,
        }

    dashboard_app = _import_app()
    monkeypatch.setattr(dashboard_app, "composition_snapshot", fake_comp)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/news-edgar/composition")
    assert resp.status_code == 200
    body = resp.json()
    assert body["news"]["total_articles"] == 27271
    assert body["filings"]["total_filings"] == 3175782
    assert body["features"][0]["status"] == "LIVE"


def test_stream_degrades_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB outage must degrade the EDGAR side to an ``error`` block, not crash the whole stream payload."""

    def raising_connect() -> object:
        raise psycopg.OperationalError("DB down")

    monkeypatch.setattr(ne, "_db_connect", raising_connect)
    # News side reads no partitions in the test env — point it at an empty glob so it returns a quiet payload.
    monkeypatch.setattr(ne, "recent_news_partition_paths", lambda limit: [])
    payload = ne.stream_snapshot()
    assert "error" in payload["edgar"]
    assert payload["news"]["window_count"] == 0
