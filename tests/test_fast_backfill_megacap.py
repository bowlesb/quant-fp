"""Unit tests for the mega-cap-aware quotes pass in the fast tick engine.

A full-day SIP quote frame for a megacap/ETF peaks at ~2.8 GB transiently, so the heaviest symbols must
run at bounded concurrency in a SEPARATE pass from the cheap long tail. These tests verify the split
(heavy head vs tail), the concurrency routing, and that trades skip the heavy pass — all without network
or a process pool (``_run_units`` is mocked to record how each pass was invoked).
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.data import fast_backfill
from quantlib.data.raw_store import write_manifest_part


@pytest.fixture
def captured_passes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace _run_units with a recorder so we observe each pass's symbols + concurrency, and replace
    _pending_units with identity-over-symbols so every (symbol, day) counts as pending.
    """
    passes: list[dict[str, object]] = []

    def fake_pending(
        store: str, tier: str, symbols: list[str], days: list[dt.date]
    ) -> list[tuple[str, str]]:
        return [(symbol, day.isoformat()) for symbol in symbols for day in days]

    def fake_run_units(
        store: str,
        tier: str,
        units: list[tuple[str, str]],
        processes: int,
        threads_per_process: int,
        total_units: int,
        totals: fast_backfill._TierTotals,
    ) -> None:
        passes.append(
            {
                "symbols": sorted({symbol for symbol, _ in units}),
                "processes": processes,
                "threads": threads_per_process,
                "n_units": len(units),
            }
        )
        totals.written += len(units)

    monkeypatch.setattr(fast_backfill, "_pending_units", fake_pending)
    monkeypatch.setattr(fast_backfill, "_run_units", fake_run_units)
    return passes


DAYS = [dt.date(2026, 6, 12)]


def test_quotes_splits_heavy_head_from_tail(
    captured_passes: list[dict[str, object]],
) -> None:
    symbols = [f"SYM{i:03d}" for i in range(100)]
    fast_backfill.run_tier_fast(
        "/store",
        "quotes",
        symbols,
        DAYS,
        processes=8,
        threads_per_process=2,
        heavy_count=10,
        heavy_processes=2,
        heavy_threads=1,
    )
    assert len(captured_passes) == 2
    heavy, tail = captured_passes
    assert heavy["symbols"] == symbols[:10]
    assert heavy["processes"] == 2 and heavy["threads"] == 1
    assert tail["symbols"] == symbols[10:]
    assert tail["processes"] == 8 and tail["threads"] == 2


def test_trades_skip_heavy_pass(captured_passes: list[dict[str, object]]) -> None:
    symbols = [f"SYM{i:03d}" for i in range(100)]
    fast_backfill.run_tier_fast(
        "/store",
        "trades",
        symbols,
        DAYS,
        processes=8,
        threads_per_process=2,
        heavy_count=10,
    )
    assert len(captured_passes) == 1
    only = captured_passes[0]
    assert only["symbols"] == symbols
    assert only["processes"] == 8 and only["threads"] == 2


def test_heavy_count_exceeding_universe_runs_only_heavy(
    captured_passes: list[dict[str, object]],
) -> None:
    symbols = ["A", "B", "C"]
    fast_backfill.run_tier_fast(
        "/store",
        "quotes",
        symbols,
        DAYS,
        heavy_count=10,
        heavy_processes=2,
        heavy_threads=1,
    )
    assert len(captured_passes) == 1
    assert captured_passes[0]["symbols"] == symbols
    assert captured_passes[0]["processes"] == 2


def test_returns_combined_written_count(
    captured_passes: list[dict[str, object]],
) -> None:
    symbols = [f"SYM{i:03d}" for i in range(30)]
    written, _ = fast_backfill.run_tier_fast(
        "/store", "quotes", symbols, DAYS, heavy_count=10
    )
    assert written == 30  # heavy 10 + tail 20, one unit per symbol-day


def test_pending_units_rows_aware_resume(tmp_path, monkeypatch) -> None:
    """The fast tick engine's resume is rows-aware: a RECENT empty (0-row) trades entry — the 06-18 poison
    — stays PENDING (re-fetched), while a real recent entry and an aged-out empty are skipped."""
    store = str(tmp_path)
    day = dt.date(2026, 6, 18)
    now = dt.datetime(2026, 6, 18, tzinfo=dt.timezone.utc)
    write_manifest_part(
        store,
        "trades",
        [
            {"tier": "trades", "symbol": "REAL", "date": "2026-06-18", "rows": 99, "bytes": 7, "fetched_at": now},
            {"tier": "trades", "symbol": "EMPTY", "date": "2026-06-18", "rows": 0, "bytes": 7, "fetched_at": now},
            {"tier": "trades", "symbol": "OLDEMPTY", "date": "2026-01-02", "rows": 0, "bytes": 7, "fetched_at": now},
        ],
        part_seq=1,
    )
    monkeypatch.setattr(fast_backfill, "_utc_today", lambda: dt.date(2026, 6, 19))
    pending = fast_backfill._pending_units(store, "trades", ["REAL", "EMPTY"], [day])
    assert pending == [("EMPTY", "2026-06-18")]  # only the recent empty is re-fetched
    # the aged-out empty is NOT pending on its own day
    old_pending = fast_backfill._pending_units(store, "trades", ["OLDEMPTY"], [dt.date(2026, 1, 2)])
    assert old_pending == []


def test_pending_units_pinned_ticker_stub_refetched(tmp_path, monkeypatch) -> None:
    """A pinned market ticker (SPY) with a tiny pre-settle stub (trades=2) stays PENDING so the fast engine
    re-fetches the full tape — the 06-18 sweep blocker — while a non-pinned name with 2 rows is done."""
    store = str(tmp_path)
    day = dt.date(2026, 6, 18)
    now = dt.datetime(2026, 6, 19, 1, 30, tzinfo=dt.timezone.utc)
    pinned = sorted(fast_backfill.FORCE_REFETCH_SYMBOLS)[0]  # SPY/QQQ
    write_manifest_part(
        store,
        "trades",
        [
            {"tier": "trades", "symbol": pinned, "date": "2026-06-18", "rows": 2, "bytes": 7, "fetched_at": now},
            {"tier": "trades", "symbol": "ILLIQ", "date": "2026-06-18", "rows": 2, "bytes": 7, "fetched_at": now},
        ],
        part_seq=1,
    )
    monkeypatch.setattr(fast_backfill, "_utc_today", lambda: dt.date(2026, 6, 19))
    pending = fast_backfill._pending_units(store, "trades", [pinned, "ILLIQ"], [day])
    assert pending == [(pinned, "2026-06-18")]  # the pinned stub re-fetched; the illiquid 2-trade day skipped
