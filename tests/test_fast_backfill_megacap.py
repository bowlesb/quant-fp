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
