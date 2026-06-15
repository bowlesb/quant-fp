"""Unit tests for the per-symbol latency drill-down's PURE top-K selector (no I/O).

``top_k_slow_symbols`` is the only logic that decides WHICH tickers land in latency_slow_symbols, so it
is tested directly: correct ordering by total_latency_s, correct K bound, correct per-row arithmetic
(arrival_lag_s vs total_latency_s), and deterministic tie-breaking.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quantlib.features.latency_drilldown import TOP_K_SLOW_SYMBOLS, top_k_slow_symbols


def _minute_epoch() -> float:
    return datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc).timestamp()


def test_picks_slowest_by_total_latency() -> None:
    minute = _minute_epoch()
    # symbol arrival = minute + delivery_lag; ready is 2.0s after the minute boundary.
    arrivals = {"AAA": minute + 0.1, "BBB": minute + 1.5, "CCC": minute + 0.5}
    ready = minute + 2.0
    rows = top_k_slow_symbols(arrivals, ready, minute, k=2)
    assert [row.symbol for row in rows] == ["AAA", "CCC"]  # smallest arrival => largest total_latency
    assert rows[0].total_latency_s == pytest.approx(ready - arrivals["AAA"])
    assert rows[0].arrival_lag_s == pytest.approx(arrivals["AAA"] - minute)


def test_arrival_lag_vs_total_latency_separates_cause() -> None:
    minute = _minute_epoch()
    # AAA delivered LATE by Alpaca (lag 5s) but cheap for us; BBB delivered fast but slow in our pipeline.
    arrivals = {"AAA": minute + 5.0, "BBB": minute + 0.2}
    ready = minute + 6.0
    rows = top_k_slow_symbols(arrivals, ready, minute, k=2)
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["AAA"].arrival_lag_s == pytest.approx(5.0)
    assert by_symbol["AAA"].total_latency_s == pytest.approx(1.0)  # ready - arrival: Alpaca-late, not us
    assert by_symbol["BBB"].arrival_lag_s == pytest.approx(0.2)
    assert by_symbol["BBB"].total_latency_s == pytest.approx(5.8)  # slow in OUR pipeline


def test_k_bounds_result() -> None:
    minute = _minute_epoch()
    arrivals = {f"S{i:05d}": minute + (i * 0.001) for i in range(1000)}
    rows = top_k_slow_symbols(arrivals, minute + 10.0, minute)
    assert len(rows) == TOP_K_SLOW_SYMBOLS
    # The slowest are the EARLIEST-arriving (largest ready-arrival); they should be S00000..S00019.
    assert [row.symbol for row in rows] == [f"S{i:05d}" for i in range(TOP_K_SLOW_SYMBOLS)]


def test_tie_break_is_deterministic_by_symbol() -> None:
    minute = _minute_epoch()
    arrivals = {"ZZZ": minute + 1.0, "AAA": minute + 1.0, "MMM": minute + 1.0}
    rows = top_k_slow_symbols(arrivals, minute + 2.0, minute, k=3)
    assert [row.symbol for row in rows] == ["AAA", "MMM", "ZZZ"]  # equal latency -> sorted by symbol


def test_empty_input_returns_empty() -> None:
    minute = _minute_epoch()
    assert top_k_slow_symbols({}, minute + 1.0, minute) == []
