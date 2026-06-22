"""Unit tests for the ALT-DATA source-dependency abstraction (docs/SOURCE_DATA_DEPENDENCY.md).

The news/edgar analogue of ``tests/test_source_dependency.py``. Cover: (1) the ``Source`` enum + the per-group
``required_sources`` / ``source_lookback_days`` DECLARATION (default-by-market-layer + alt-data override);
(2) news hole detection against the real ``/store/news`` manifest (empty store, backfilled-date resume) —
reusing the news backfill's ``backfilled_dates`` resume key; (3) edgar hole detection (coverage set + the
recent-vs-aged settle-window rule), with the DB coverage query monkeypatched (no Postgres); (4) the
``ensure_sources`` orchestration (fetch only holes, share-the-source no-op, lock-held skip, no-fetcher skip,
dry-run); (5) the group-resolution helpers (union, alt subset, deepest lookback, horizon expansion).

No DB and no Alpaca: the lock runs in ``dry_run`` (or is monkeypatched to grant), the edgar coverage query is
monkeypatched, and the fetcher is a recording stub. The news manifest is seeded with the real
``news_store.write_manifest_part`` so news hole detection is exercised end-to-end against on-disk parts.
"""
from __future__ import annotations

import datetime as dt

from quantlib.data import source_inputs
from quantlib.data.news_store import SRC_BACKFILL, SRC_LIVE, write_manifest_part
from quantlib.data.source_dependency import SourceIngestLock
from quantlib.data.source_inputs import (
    EnsureSourcesReport,
    alt_sources_for_groups,
    ensure_sources,
    find_edgar_holes,
    find_news_holes,
    horizon_dates,
    required_sources_for_groups,
    source_lookback_days_for_groups,
)
from quantlib.features.base import (
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    RawLayer,
    Source,
)

TODAY = dt.date(2026, 6, 21)
RECENT = dt.date(2026, 6, 20)  # within the edgar 1-day settle window of TODAY
AGED = dt.date(2026, 5, 1)  # well outside any settle window


class _NewsGroup(FeatureGroup):
    name = "_test_news_group"
    version = "v1"
    owner = "test"
    type = FeatureType.REFERENCE

    def required_sources(self) -> frozenset[Source]:
        return frozenset({Source.NEWS})

    def source_lookback_days(self, source: Source) -> int:
        return 9 if source is Source.NEWS else 0

    def declare(self) -> list[FeatureSpec]:
        return [FeatureSpec(name="x", description="a" * 41, dtype="Float64")]

    def compute(self, ctx):  # pragma: no cover - not exercised
        raise NotImplementedError


class _EdgarGroup(_NewsGroup):
    name = "_test_edgar_group"

    def required_sources(self) -> frozenset[Source]:
        return frozenset({Source.EDGAR})

    def source_lookback_days(self, source: Source) -> int:
        return 370 if source is Source.EDGAR else 0


class _MarketGroup(_NewsGroup):
    name = "_test_market_group"
    type = FeatureType.QUOTE_SPREAD

    def required_sources(self) -> frozenset[Source]:
        # Use the real default (lift required_raw_layers into Source) — no alt source.
        return FeatureGroup.required_sources(self)

    def source_lookback_days(self, source: Source) -> int:
        return 0


class _RecordingFetcher:
    """Records the (symbols, dates) it is asked to fetch and reports a plausible fetched-date count."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[dt.date]]] = []

    def __call__(self, symbols: list[str], days: list[dt.date]) -> int:
        self.calls.append((list(symbols), list(days)))
        return len(days)


def _patch_groups(monkeypatch, groups: dict[str, FeatureGroup]) -> None:
    monkeypatch.setattr(source_inputs.REGISTRY, "get_group", lambda name: groups[name])


def _grant_lock(monkeypatch) -> None:
    monkeypatch.setattr(SourceIngestLock, "claim", lambda self, source: True)
    monkeypatch.setattr(SourceIngestLock, "release", lambda self, source: True)


def _seed_news_manifest(store: str, date_iso: str, source: str) -> None:
    write_manifest_part(
        store,
        [
            {
                "published_date": date_iso,
                "articles": 0,
                "bytes": 0,
                "source": source,
                "fetched_at": dt.datetime(2026, 6, 21, tzinfo=dt.timezone.utc),
            }
        ],
        source,
    )


def test_source_enum_market_and_alt() -> None:
    assert Source.BARS.value == RawLayer.BARS.value == "bars"
    assert {Source.NEWS.value, Source.EDGAR.value} == {"news", "edgar"}
    assert Source.BARS.is_market_layer and Source.TRADES.is_market_layer
    assert not Source.NEWS.is_market_layer and not Source.EDGAR.is_market_layer
    assert Source.QUOTES.as_raw_layer() is RawLayer.QUOTES


def test_news_as_raw_layer_raises() -> None:
    try:
        Source.NEWS.as_raw_layer()
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_news_group_declares_news_source_and_lookback() -> None:
    group = _NewsGroup()
    assert group.required_sources() == frozenset({Source.NEWS})
    assert group.source_lookback_days(Source.NEWS) == 9
    assert group.source_lookback_days(Source.EDGAR) == 0


def test_edgar_group_declares_edgar_source_and_lookback() -> None:
    group = _EdgarGroup()
    assert group.required_sources() == frozenset({Source.EDGAR})
    assert group.source_lookback_days(Source.EDGAR) == 370


def test_market_group_default_lifts_raw_layers_no_alt() -> None:
    """A market group needs no edit to be source-aware: required_sources lifts its raw layers into Source and
    adds no alt-data source."""
    sources = _MarketGroup().required_sources()
    assert sources == frozenset({Source.BARS, Source.TRADES, Source.QUOTES})
    assert all(source.is_market_layer for source in sources)


def test_required_sources_for_groups_unions(monkeypatch) -> None:
    _patch_groups(
        monkeypatch,
        {"n": _NewsGroup(), "e": _EdgarGroup(), "m": _MarketGroup()},
    )
    union = required_sources_for_groups(["n", "e", "m"])
    assert union == frozenset({Source.NEWS, Source.EDGAR, Source.BARS, Source.TRADES, Source.QUOTES})


def test_alt_sources_for_groups_drops_market(monkeypatch) -> None:
    _patch_groups(monkeypatch, {"n": _NewsGroup(), "e": _EdgarGroup(), "m": _MarketGroup()})
    assert alt_sources_for_groups(["n", "e", "m"]) == frozenset({Source.NEWS, Source.EDGAR})


def test_source_lookback_days_for_groups_is_deepest(monkeypatch) -> None:
    _patch_groups(monkeypatch, {"n": _NewsGroup(), "e": _EdgarGroup()})
    assert source_lookback_days_for_groups(["n"], Source.NEWS) == 9
    assert source_lookback_days_for_groups(["e"], Source.EDGAR) == 370
    # A group that does not consume the source contributes 0 (not an error).
    assert source_lookback_days_for_groups(["n", "e"], Source.NEWS) == 9


def test_horizon_dates_expands_by_lookback() -> None:
    days = horizon_dates(dt.date(2026, 6, 16), dt.date(2026, 6, 18), 9)
    assert days[0] == dt.date(2026, 6, 7)
    assert days[-1] == dt.date(2026, 6, 18)
    assert len(days) == 12


def test_find_news_holes_empty_store_all_dates_are_holes(tmp_path) -> None:
    store = str(tmp_path)
    days = [AGED, RECENT]
    holes = find_news_holes(store, days)
    assert holes.source is Source.NEWS
    assert holes.dates == days


def test_find_news_holes_skips_backfilled_dates(tmp_path) -> None:
    """A date a BACKFILL manifest part recorded is done (the news resume key); a LIVE-only part is NOT a
    completion signal, so a live-touched-but-never-backfilled date is still a hole."""
    store = str(tmp_path)
    _seed_news_manifest(store, AGED.isoformat(), SRC_BACKFILL)  # backfill-seeded → done
    _seed_news_manifest(store, RECENT.isoformat(), SRC_LIVE)  # live-only → still a hole
    holes = find_news_holes(store, [AGED, RECENT])
    assert holes.dates == [RECENT]


def test_find_edgar_holes_recent_uncovered_is_hole_aged_is_not(monkeypatch) -> None:
    """An uncovered RECENT day (within the settle window) is a hole (re-checked); an uncovered AGED day is a
    genuine empty SEC day (weekend/holiday), NOT a permanent hole."""
    monkeypatch.setattr(source_inputs, "edgar_covered_dates", lambda days: set())
    holes = find_edgar_holes([AGED, RECENT], today=TODAY, settle_window_days=1)
    assert holes.source is Source.EDGAR
    assert holes.dates == [RECENT]


def test_find_edgar_holes_covered_day_is_not_a_hole(monkeypatch) -> None:
    monkeypatch.setattr(source_inputs, "edgar_covered_dates", lambda days: {RECENT})
    holes = find_edgar_holes([RECENT], today=TODAY, settle_window_days=1)
    assert holes.is_empty


def test_ensure_sources_dry_run_reports_without_fetching(tmp_path) -> None:
    store = str(tmp_path)
    fetcher = _RecordingFetcher()
    report = ensure_sources(
        store,
        frozenset({Source.NEWS}),
        ["AAPL"],
        [AGED],
        agent_id="job-1",
        fetchers={Source.NEWS: fetcher},
        today=TODAY,
        dry_run=True,
    )
    assert report.holes_before[Source.NEWS] == 1
    assert fetcher.calls == []
    assert report.fetched_dates[Source.NEWS] == 0
    assert report.all_present


def test_ensure_sources_fetches_only_news_holes(tmp_path, monkeypatch) -> None:
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    _seed_news_manifest(store, AGED.isoformat(), SRC_BACKFILL)  # AGED done; only RECENT is a hole
    fetcher = _RecordingFetcher()
    report = ensure_sources(
        store,
        frozenset({Source.NEWS}),
        ["AAPL", "MSFT"],
        [AGED, RECENT],
        agent_id="job-1",
        fetchers={Source.NEWS: fetcher},
        today=TODAY,
        dry_run=False,
    )
    assert report.holes_before[Source.NEWS] == 1
    assert len(fetcher.calls) == 1
    symbols, days = fetcher.calls[0]
    assert symbols == ["AAPL", "MSFT"]
    assert days == [RECENT]  # AGED was not re-fetched (shared source)
    assert report.fetched_dates[Source.NEWS] == 1


def test_ensure_sources_current_source_is_a_noop(tmp_path, monkeypatch) -> None:
    """Benefit (A): a second backfill over an already-seeded horizon fetches nothing — idempotent."""
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    for day in (AGED, RECENT):
        _seed_news_manifest(store, day.isoformat(), SRC_BACKFILL)
    fetcher = _RecordingFetcher()
    report = ensure_sources(
        store,
        frozenset({Source.NEWS}),
        ["AAPL"],
        [AGED, RECENT],
        agent_id="job-1",
        fetchers={Source.NEWS: fetcher},
        today=TODAY,
        dry_run=False,
    )
    assert report.holes_before[Source.NEWS] == 0
    assert fetcher.calls == []


def test_ensure_sources_skips_source_when_lock_held(tmp_path, monkeypatch) -> None:
    store = str(tmp_path)
    fetcher = _RecordingFetcher()
    monkeypatch.setattr(SourceIngestLock, "claim", lambda self, source: False)
    report = ensure_sources(
        store,
        frozenset({Source.NEWS}),
        ["AAPL"],
        [AGED],
        agent_id="job-2",
        fetchers={Source.NEWS: fetcher},
        today=TODAY,
        dry_run=False,
    )
    assert fetcher.calls == []
    assert report.skipped_locked == (Source.NEWS,)
    assert not report.all_present


def test_ensure_sources_no_fetcher_source_is_reported_not_fetched(tmp_path, monkeypatch) -> None:
    """EDGAR has no in-process fetcher (its submissions backfill is the services/edgar operator job): the
    source is hole-detected + reported but skipped (so a require-all caller sees it is not self-served)."""
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    monkeypatch.setattr(source_inputs, "edgar_covered_dates", lambda days: set())
    report = ensure_sources(
        store,
        frozenset({Source.EDGAR}),
        ["AAPL"],
        [RECENT],
        agent_id="job-1",
        fetchers={},  # no edgar fetcher
        today=TODAY,
        dry_run=False,
    )
    assert report.holes_before[Source.EDGAR] == 1
    assert report.fetched_dates[Source.EDGAR] == 0
    assert report.skipped_locked == (Source.EDGAR,)


def test_ensure_sources_multi_source_each_handled(tmp_path, monkeypatch) -> None:
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    monkeypatch.setattr(source_inputs, "edgar_covered_dates", lambda days: set())
    news_fetcher = _RecordingFetcher()
    report = ensure_sources(
        store,
        frozenset({Source.NEWS, Source.EDGAR}),
        ["AAPL"],
        [RECENT],
        agent_id="job-1",
        fetchers={Source.NEWS: news_fetcher},
        today=TODAY,
        dry_run=False,
    )
    assert report.sources == (Source.EDGAR, Source.NEWS)  # sorted by value
    assert len(news_fetcher.calls) == 1  # news fetched
    assert report.skipped_locked == (Source.EDGAR,)  # edgar reported, no in-process fetcher


def test_empty_alt_sources_report_is_present() -> None:
    """A backfill of only market groups has no alt sources to ensure — an empty, all-present report."""
    report = EnsureSourcesReport(sources=())
    assert report.all_present
    assert report.sources == ()
