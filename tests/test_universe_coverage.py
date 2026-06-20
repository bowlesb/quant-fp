"""Unit tests for the UNIVERSE coverage surface (services/dashboard/universe_coverage).

No live DB: the two reads (``_read_captured_per_day`` / ``_read_asset_metadata``) are passed in as fixtures
(the pure ``build_universe_coverage`` takes them as args), so the aggregation — the available-set screen, the
per-day captured/available ratio + status bands, the uncaptured gap, and the pre-screen over-available flag —
is exercised end-to-end against a controlled universe.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import universe_coverage as uc  # noqa: E402  (path inserted above)


def _captured(date: str, n: int) -> dict[str, object]:
    """One ``_read_captured_per_day`` row: a session date + its in_universe count."""
    return {"trade_date": dt.date.fromisoformat(date), "n_captured": n}


def _asset(symbol: str, name: str | None, exchange: str, tradable: bool) -> dict[str, object]:
    """One ``_read_asset_metadata`` row for the available-set screen."""
    return {"symbol": symbol, "name": name, "exchange": exchange, "tradable": tradable}


# A controlled asset_metadata snapshot: 4 survive the screen (NASDAQ/NYSE common stock), and each of the
# others is excluded by exactly one rule (ETF-like name / off-venue / non-tradable / slash symbol).
_ASSETS = [
    _asset("AAPL", "Apple Inc.", "NASDAQ", True),
    _asset("MSFT", "Microsoft Corp", "NASDAQ", True),
    _asset("JPM", "JPMorgan Chase", "NYSE", True),
    _asset("F", "Ford Motor Co", "NYSE", True),
    _asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA", True),  # ETF-like name -> excluded
    _asset("OTCX", "Some OTC Co", "OTC", True),  # off-venue -> excluded
    _asset("DEAD", "Delisted Co", "NASDAQ", False),  # not tradable -> excluded
    _asset("BRK/B", "Berkshire Hathaway", "NYSE", True),  # slash symbol -> excluded
]
_AVAILABLE = 4


def test_available_filtered_count_reproduces_seed_screen() -> None:
    assert uc.available_filtered_count(_ASSETS) == _AVAILABLE


def test_ratio_and_status_bands() -> None:
    captured = [_captured("2026-06-22", 4), _captured("2026-06-19", 3), _captured("2026-06-18", 2)]
    view = uc.build_universe_coverage(captured_rows=captured, asset_rows=_ASSETS)
    assert view["available"] == 4
    timeline = view["timeline"]
    # newest first; 4/4=full, 3/4=thinned (>=0.6), 2/4=capped (<0.6)
    assert [t["date"] for t in timeline] == ["2026-06-22", "2026-06-19", "2026-06-18"]
    assert [t["status"] for t in timeline] == ["full", "thinned", "capped"]
    assert timeline[0]["ratio"] == 1.0
    assert timeline[1]["ratio_pct"] == 75.0
    assert timeline[2]["uncaptured"] == 2


def test_latest_and_headline_status() -> None:
    captured = [_captured("2026-06-22", 3), _captured("2026-06-19", 4)]
    view = uc.build_universe_coverage(captured_rows=captured, asset_rows=_ASSETS)
    latest = view["latest"]
    assert latest is not None
    assert latest["date"] == "2026-06-22"
    assert latest["captured"] == 3
    assert latest["uncaptured"] == 1
    assert view["status"] == "thinned"  # mirrors the latest row


def test_over_available_pre_screen_seed_flagged() -> None:
    # A day captured ABOVE the current available set (e.g. 06-15's pre-ETF-screen 11336 seed) is flagged and
    # its ratio clamped to 1.0, not shown as >100%.
    captured = [_captured("2026-06-15", 9)]
    view = uc.build_universe_coverage(captured_rows=captured, asset_rows=_ASSETS)
    row = view["timeline"][0]
    assert row["over_available"] is True
    assert row["ratio"] == 1.0
    assert row["ratio_pct"] == 100.0
    assert row["uncaptured"] == 0


def test_thresholds_exposed() -> None:
    view = uc.build_universe_coverage(captured_rows=[_captured("2026-06-22", 4)], asset_rows=_ASSETS)
    assert view["ratio_thresholds"] == {"ok": uc.RATIO_OK, "thin": uc.RATIO_THIN}


def test_empty_captured_is_safe() -> None:
    view = uc.build_universe_coverage(captured_rows=[], asset_rows=_ASSETS)
    assert view["available"] == 4
    assert view["latest"] is None
    assert view["status"] == "unknown"
    assert view["timeline"] == []


def test_no_available_assets_ratio_zero() -> None:
    # Degenerate: no asset survives the screen -> available 0, ratio 0 (no divide-by-zero), status capped.
    view = uc.build_universe_coverage(
        captured_rows=[_captured("2026-06-22", 5)],
        asset_rows=[_asset("SPY", "SPDR ETF Trust", "ARCA", True)],
    )
    assert view["available"] == 0
    row = view["timeline"][0]
    assert row["ratio"] == 0.0
    assert row["over_available"] is False
    assert row["status"] == "capped"
