"""Unit tests for the source-data dependency abstraction (docs/SOURCE_DATA_DEPENDENCY.md).

Cover: (1) the per-group raw-layer DECLARATION (default-by-type + override); (2) hole detection against the
real manifest (empty store, partial fill, settle-window poison re-fetch, aged-out skip) — reusing the
acquire-side ``resumable_done_keys`` policy; (3) the ``ensure_inputs`` orchestration (fetch only holes,
share-the-source no-op, idempotent re-run, lock-held skip); (4) the single-writer lock dry-run contract.

No DB and no Alpaca: the lock runs in ``dry_run`` and the fetcher is a recording stub. The manifest is
seeded with the real ``write_manifest_part`` so hole detection is exercised end-to-end against on-disk parts.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.data.raw_store import MANIFEST_SCHEMA, write_manifest_part
from quantlib.data.source_dependency import (SourceIngestLock, ensure_inputs,
                                             find_holes,
                                             required_layers_for_groups)
from quantlib.features.base import (FeatureGroup, FeatureSpec, FeatureType,
                                    RawLayer)

TODAY = dt.date(2026, 6, 21)
RECENT = dt.date(2026, 6, 18)  # within the 5-day settle window
AGED = dt.date(2026, 5, 1)  # well outside the settle window


def _manifest_entry(tier: str, symbol: str, day: dt.date, rows: int) -> dict:
    return {
        "tier": tier,
        "symbol": symbol,
        "date": day.isoformat(),
        "rows": rows,
        "bytes": max(rows, 0),
        "fetched_at": dt.datetime(2026, 6, 21, tzinfo=dt.timezone.utc),
    }


def _seed_manifest(store: str, tier: str, entries: list[dict]) -> None:
    write_manifest_part(store, tier, entries, part_seq=0)


class _RecordingFetcher:
    """Records the (layer, symbols, days) it is asked to fetch and reports plausible written counts."""

    def __init__(self) -> None:
        self.calls: list[tuple[RawLayer, list[str], list[dt.date]]] = []

    def __call__(self, layer: RawLayer, symbols: list[str], days: list[dt.date]) -> tuple[int, int]:
        self.calls.append((layer, list(symbols), list(days)))
        n_units = len(symbols) * len(days)
        return n_units, n_units * 1000


class _DefaultLayerGroup(FeatureGroup):
    name = "_test_default_layer"
    version = "v1"
    owner = "test"
    type = FeatureType.PRICE

    def declare(self) -> list[FeatureSpec]:
        return [FeatureSpec(name="x", description="a" * 41, dtype="Float64")]

    def compute(self, ctx):  # pragma: no cover - not exercised
        raise NotImplementedError


class _QuoteLayerGroup(_DefaultLayerGroup):
    name = "_test_quote_layer"
    type = FeatureType.QUOTE_SPREAD


class _OverrideLayerGroup(_DefaultLayerGroup):
    name = "_test_override_layer"
    type = FeatureType.PRICE  # default would be {bars}, but this group really needs trades too

    def required_raw_layers(self) -> frozenset[RawLayer]:
        return frozenset({RawLayer.BARS, RawLayer.TRADES})


def test_default_raw_layer_is_bars_for_a_bar_group() -> None:
    assert _DefaultLayerGroup().required_raw_layers() == frozenset({RawLayer.BARS})


def test_quote_group_default_needs_all_three_layers() -> None:
    assert _QuoteLayerGroup().required_raw_layers() == frozenset(
        {RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES}
    )


def test_group_can_override_its_raw_layers() -> None:
    assert _OverrideLayerGroup().required_raw_layers() == frozenset(
        {RawLayer.BARS, RawLayer.TRADES}
    )


def test_required_layers_for_groups_unions(monkeypatch) -> None:
    groups = {
        "_test_default_layer": _DefaultLayerGroup(),
        "_test_quote_layer": _QuoteLayerGroup(),
    }
    monkeypatch.setattr(
        "quantlib.data.source_dependency.REGISTRY.get_group", lambda name: groups[name]
    )
    union = required_layers_for_groups(["_test_default_layer", "_test_quote_layer"])
    assert union == frozenset({RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES})


def test_find_holes_empty_store_all_units_are_holes(tmp_path) -> None:
    store = str(tmp_path)
    holes = find_holes(store, RawLayer.BARS, ["AAPL", "MSFT"], [AGED, RECENT], today=TODAY)
    assert holes.layer is RawLayer.BARS
    assert not holes.is_empty
    assert set(holes.units) == {
        ("AAPL", AGED.isoformat()),
        ("AAPL", RECENT.isoformat()),
        ("MSFT", AGED.isoformat()),
        ("MSFT", RECENT.isoformat()),
    }


def test_find_holes_skips_present_keys(tmp_path) -> None:
    store = str(tmp_path)
    _seed_manifest(store, "bars", [_manifest_entry("bars", "AAPL", AGED, rows=390)])
    holes = find_holes(store, RawLayer.BARS, ["AAPL", "MSFT"], [AGED], today=TODAY)
    # AAPL@AGED is present; only MSFT@AGED remains a hole.
    assert holes.units == [("MSFT", AGED.isoformat())]


def test_find_holes_recent_zero_row_entry_is_a_hole(tmp_path) -> None:
    """A premature/unsettled fetch recorded 0 rows for a RECENT day → still a hole (re-fetched), matching
    the acquire-side ``resumable_done_keys`` poison rule. An AGED 0-row day is NOT a hole (genuine no-data)."""
    store = str(tmp_path)
    _seed_manifest(
        store,
        "trades",
        [
            _manifest_entry("trades", "AAPL", RECENT, rows=0),  # unsettled → re-fetch
            _manifest_entry("trades", "AAPL", AGED, rows=0),  # aged no-data → done
        ],
    )
    holes = find_holes(store, RawLayer.TRADES, ["AAPL"], [RECENT, AGED], today=TODAY)
    assert holes.units == [("AAPL", RECENT.isoformat())]


def _grant_lock(monkeypatch) -> None:
    """Make the DB-backed lock a no-op GRANT (claim/release succeed without a real DB), so the live
    (``dry_run=False``) fetch path can be exercised without Postgres."""
    monkeypatch.setattr(SourceIngestLock, "claim", lambda self, layer: True)
    monkeypatch.setattr(SourceIngestLock, "release", lambda self, layer: True)


def test_ensure_inputs_dry_run_reports_holes_without_fetching(tmp_path) -> None:
    """dry_run reports what WOULD be fetched but never calls the fetcher (never hits Alpaca)."""
    store = str(tmp_path)
    fetcher = _RecordingFetcher()
    report = ensure_inputs(
        store,
        frozenset({RawLayer.BARS}),
        ["AAPL"],
        [AGED],
        agent_id="job-1",
        fetcher=fetcher,
        today=TODAY,
        dry_run=True,
    )
    assert report.holes_before[RawLayer.BARS] == 1
    assert fetcher.calls == []  # dry-run does not download
    assert report.fetched_units[RawLayer.BARS] == 0
    assert report.all_present


def test_ensure_inputs_fetches_only_holes(tmp_path, monkeypatch) -> None:
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    # AAPL@AGED already present; MSFT@AGED is the only hole.
    _seed_manifest(store, "bars", [_manifest_entry("bars", "AAPL", AGED, rows=390)])
    fetcher = _RecordingFetcher()
    report = ensure_inputs(
        store,
        frozenset({RawLayer.BARS}),
        ["AAPL", "MSFT"],
        [AGED],
        agent_id="job-1",
        fetcher=fetcher,
        today=TODAY,
        dry_run=False,
    )
    assert report.holes_before[RawLayer.BARS] == 1
    assert len(fetcher.calls) == 1
    layer, symbols, days = fetcher.calls[0]
    assert layer is RawLayer.BARS
    assert symbols == ["MSFT"]  # AAPL was not re-fetched (shared source)
    assert days == [AGED]


def test_ensure_inputs_complete_tape_is_a_noop(tmp_path, monkeypatch) -> None:
    """Benefit (A): a second backfill over an already-present horizon fetches nothing — idempotent."""
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    _seed_manifest(
        store,
        "bars",
        [
            _manifest_entry("bars", "AAPL", AGED, rows=390),
            _manifest_entry("bars", "MSFT", AGED, rows=390),
        ],
    )
    fetcher = _RecordingFetcher()
    report = ensure_inputs(
        store,
        frozenset({RawLayer.BARS}),
        ["AAPL", "MSFT"],
        [AGED],
        agent_id="job-1",
        fetcher=fetcher,
        today=TODAY,
        dry_run=False,
    )
    assert report.holes_before[RawLayer.BARS] == 0
    assert fetcher.calls == []
    assert report.fetched_units[RawLayer.BARS] == 0


def test_ensure_inputs_multi_layer_fetches_each_layer_once(tmp_path, monkeypatch) -> None:
    store = str(tmp_path)
    _grant_lock(monkeypatch)
    fetcher = _RecordingFetcher()
    report = ensure_inputs(
        store,
        frozenset({RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES}),
        ["AAPL"],
        [AGED],
        agent_id="job-1",
        fetcher=fetcher,
        today=TODAY,
        dry_run=False,
    )
    fetched_layers = {layer for layer, _, _ in fetcher.calls}
    assert fetched_layers == {RawLayer.BARS, RawLayer.TRADES, RawLayer.QUOTES}
    assert report.layers == (RawLayer.BARS, RawLayer.QUOTES, RawLayer.TRADES)  # sorted by value


def test_ensure_inputs_skips_layer_when_lock_held(tmp_path, monkeypatch) -> None:
    """A layer whose ingest lock is held by another live job is SKIPPED (recorded), not blocked — the
    serialization guarantee (no two writers on one layer's manifest) without a hang."""
    store = str(tmp_path)
    fetcher = _RecordingFetcher()
    # Force the lock claim to fail (another job holds it).
    monkeypatch.setattr(SourceIngestLock, "claim", lambda self, layer: False)
    report = ensure_inputs(
        store,
        frozenset({RawLayer.BARS}),
        ["AAPL"],
        [AGED],
        agent_id="job-2",
        fetcher=fetcher,
        today=TODAY,
        dry_run=False,  # exercise the real lock path (claim monkeypatched, no DB)
    )
    assert fetcher.calls == []
    assert report.skipped_locked == (RawLayer.BARS,)
    assert not report.all_present


def test_lock_dry_run_does_not_touch_db() -> None:
    lock = SourceIngestLock(agent_id="job-1", dry_run=True)
    assert lock.claim(RawLayer.BARS) is True
    assert lock.heartbeat(RawLayer.BARS) is True
    assert lock.release(RawLayer.BARS) is True
    assert lock.reclaim_stale() == []


def test_manifest_schema_round_trip(tmp_path) -> None:
    """Sanity: a seeded manifest part is readable with the declared schema (guards the test helper)."""
    store = str(tmp_path)
    _seed_manifest(store, "bars", [_manifest_entry("bars", "AAPL", AGED, rows=390)])
    holes = find_holes(store, RawLayer.BARS, ["AAPL"], [AGED], today=TODAY)
    assert holes.is_empty
    assert set(MANIFEST_SCHEMA) == {"tier", "symbol", "date", "rows", "bytes", "fetched_at"}
    assert pl.DataFrame(schema=MANIFEST_SCHEMA).height == 0
