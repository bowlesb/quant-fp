"""Tests for the pure universe-selection logic."""
from quantlib.universe import SymbolStats, select_universe


def test_filters_price_and_adv() -> None:
    stats = [
        SymbolStats("PENNY", price=2.0, adv_dollar=50_000_000),   # too cheap
        SymbolStats("THIN", price=50.0, adv_dollar=1_000_000),    # too illiquid
        SymbolStats("GOOD", price=100.0, adv_dollar=80_000_000),  # passes
    ]
    chosen = [s.symbol for s in select_universe(stats)]
    assert chosen == ["GOOD"]


def test_ranks_by_adv_and_caps() -> None:
    stats = [
        SymbolStats(f"S{i}", price=10.0, adv_dollar=float(20_000_000 + i))
        for i in range(10)
    ]
    chosen = select_universe(stats, max_symbols=3)
    assert [s.symbol for s in chosen] == ["S9", "S8", "S7"]  # most liquid first


def test_deterministic_tie_break() -> None:
    stats = [
        SymbolStats("BBB", price=10.0, adv_dollar=20_000_000),
        SymbolStats("AAA", price=10.0, adv_dollar=20_000_000),
    ]
    chosen = [s.symbol for s in select_universe(stats, max_symbols=2)]
    assert chosen == ["AAA", "BBB"]  # tie broken by symbol, reproducibly
