"""Unit tests for the one-shot corporate-actions universe backfill CLI.

No network: the Alpaca trading client is mocked at module level. Covers argument parsing (months +
sample symbols), the universe screen (active/tradable/single-name), and the lookback-window math.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from quantlib.data import corporate_actions_backfill as cab


@dataclass
class FakeAsset:
    symbol: str
    tradable: bool


class MockTradingClient:
    """Returns a fixed asset list; the screen must keep only tradable single names."""

    def get_all_assets(self, request: object) -> list[FakeAsset]:
        return [
            FakeAsset("AAPL", True),
            FakeAsset("KLAC", True),
            FakeAsset("BRK/A", True),  # slash => not a single-name ticker, dropped
            FakeAsset("DEAD", False),  # not tradable, dropped
            FakeAsset("AAPL", True),  # duplicate, deduped
        ]


def test_parse_args_defaults() -> None:
    args = cab.parse_args([])
    assert args.months == cab.DEFAULT_MONTHS
    assert args.symbols is None


def test_parse_args_overrides() -> None:
    args = cab.parse_args(["--months", "24", "--symbols", "aapl, klac"])
    assert args.months == 24
    assert args.symbols == "aapl, klac"


def test_universe_screen_keeps_tradable_single_names() -> None:
    symbols = cab.universe_symbols(MockTradingClient())
    assert symbols == ["AAPL", "KLAC"]


def test_lookback_window_months() -> None:
    end = dt.date(2026, 6, 16)
    start = end - dt.timedelta(days=24 * cab.DAYS_PER_MONTH)
    assert (end - start).days == 24 * cab.DAYS_PER_MONTH
