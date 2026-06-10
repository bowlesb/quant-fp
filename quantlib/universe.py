"""Universe selection: pure, testable logic for the daily liquid-symbol screen.

The I/O (fetching daily bars from Alpaca) lives in the scheduler; this module
just turns per-symbol stats into a ranked, filtered membership list. Storing the
result per day in universe_membership gives backtests a point-in-time universe
(no survivorship bias).
"""
from dataclasses import dataclass


@dataclass
class SymbolStats:
    symbol: str
    price: float          # latest close
    adv_dollar: float     # average daily dollar volume over the lookback window


def select_universe(
    stats: list[SymbolStats],
    min_price: float = 5.0,
    min_adv_dollar: float = 10_000_000.0,
    max_symbols: int = 1000,
) -> list[SymbolStats]:
    """Filter by price and average dollar volume, then keep the most liquid
    `max_symbols` by ADV. Deterministic: ties broken by symbol so the universe is
    reproducible for a given input."""
    eligible = [
        s for s in stats
        if s.price >= min_price and s.adv_dollar >= min_adv_dollar
    ]
    eligible.sort(key=lambda s: (-s.adv_dollar, s.symbol))
    return eligible[:max_symbols]
