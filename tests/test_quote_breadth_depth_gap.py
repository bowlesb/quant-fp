"""Unit tests for the breadth-at-depth quote gap computation (quantlib.data.quote_breadth_depth_gap).

The fill target is the broad-era names (real quote tape on a settled broad reference date) whose tape
does NOT yet reach back to an earlier target window's start — i.e. the names to extend BACKWARD. These
tests build a tmp store with a controlled quotes manifest (broad ref date + earlier coverage for some
names) and assert the computed set is exactly the broad universe minus the names already covering the
window start, market tickers excluded, rows==0 entries ignored.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.data import quote_breadth_depth_gap
from quantlib.data.raw_store import write_manifest_part

BROAD_REF = "2026-03-23"
WINDOW_START = "2026-01-02"


def _seed_quote(store: str, symbol: str, date: str, rows: int) -> None:
    write_manifest_part(
        store,
        "quotes",
        [
            {
                "tier": "quotes",
                "symbol": symbol,
                "date": date,
                "rows": rows,
                "bytes": max(rows, 0),
                "fetched_at": dt.datetime(2026, 3, 23, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=hash((symbol, date)) % 100000,
    )


def test_broad_universe_real_tape_only(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_quote(store, "AAA", BROAD_REF, 100)
    _seed_quote(store, "BBB", BROAD_REF, 0)  # recorded but no real tape -> excluded
    assert quote_breadth_depth_gap.broad_universe(store, BROAD_REF) == {"AAA"}


def test_broad_universe_excludes_market_tickers(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_quote(store, "AAA", BROAD_REF, 100)
    _seed_quote(store, "SPY", BROAD_REF, 100)  # market ticker -> excluded
    universe = quote_breadth_depth_gap.broad_universe(store, BROAD_REF)
    assert "AAA" in universe
    assert "SPY" not in universe


def test_symbols_covering_date_reads_through_window_start(
    tmp_path: pytest.TempPathFactory,
) -> None:
    store = str(tmp_path)
    # HEAD already has a real tape ON the window start -> covers it.
    _seed_quote(store, "HEAD", WINDOW_START, 100)
    # LATER's earliest tape is after the window start -> does NOT cover it.
    _seed_quote(store, "LATER", "2026-03-18", 100)
    covering = quote_breadth_depth_gap.symbols_covering_date(store, WINDOW_START)
    assert covering == {"HEAD"}


def test_covering_ignores_zero_row_entries(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_quote(store, "ZERO", WINDOW_START, 0)  # rows==0 -> not real coverage
    assert quote_breadth_depth_gap.symbols_covering_date(store, WINDOW_START) == set()


def test_compute_gap_is_broad_minus_already_covering(
    tmp_path: pytest.TempPathFactory,
) -> None:
    store = str(tmp_path)
    # Broad universe on the ref date: HEAD, MIDA, MIDB (all real tape on BROAD_REF).
    for symbol in ["HEAD", "MIDA", "MIDB"]:
        _seed_quote(store, symbol, BROAD_REF, 100)
    # HEAD also reaches back to the window start -> already covered -> dropped.
    _seed_quote(store, "HEAD", WINDOW_START, 100)
    gap = quote_breadth_depth_gap.compute_breadth_depth_gap(store, WINDOW_START, broad_ref_date=BROAD_REF)
    assert gap == ["MIDA", "MIDB"]  # sorted, broad minus already-covering


def test_compute_gap_empty_when_all_broad_already_cover(
    tmp_path: pytest.TempPathFactory,
) -> None:
    store = str(tmp_path)
    for symbol in ["AAA", "BBB"]:
        _seed_quote(store, symbol, BROAD_REF, 100)
        _seed_quote(store, symbol, WINDOW_START, 100)  # already reaches back
    assert (
        quote_breadth_depth_gap.compute_breadth_depth_gap(store, WINDOW_START, broad_ref_date=BROAD_REF)
        == []
    )


def test_compute_gap_empty_on_empty_store(tmp_path: pytest.TempPathFactory) -> None:
    assert (
        quote_breadth_depth_gap.compute_breadth_depth_gap(
            str(tmp_path), WINDOW_START, broad_ref_date=BROAD_REF
        )
        == []
    )
