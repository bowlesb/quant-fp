"""Tests for the pure universe-selection logic."""

from quantlib.universe import SymbolStats, is_etf_like, select_universe


def test_filters_price_and_adv() -> None:
    stats = [
        SymbolStats("PENNY", price=2.0, adv_dollar=50_000_000),  # too cheap
        SymbolStats("THIN", price=50.0, adv_dollar=1_000_000),  # too illiquid
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


def test_is_etf_like_classifies_funds_not_stocks() -> None:
    # ETFs / leveraged-inverse / VIX / commodity & crypto pools — must be detected, including
    # the ones whose names lack the literal word "ETF".
    for name in [
        "SPDR S&P 500 ETF Trust",
        "Invesco QQQ Trust, Series 1",  # plain + sponsor-only
        "ProShares UltraPro QQQ",
        "Direxion Daily Semiconductor Bull 3X ETF",
        "iPath Series B S&P 500 VIX Short-Term Futures ETN",
        "United States Oil Fund, LP",
        "Sprott Physical Silver Trust",
        "Fidelity Wise Origin Bitcoin Fund",
        "Grayscale Bitcoin Trust",
    ]:
        assert is_etf_like(name), name
    # Real single-name equities (incl. ADRs / banks / REITs whose names contain "Trust") — kept.
    for name in [
        "Apple Inc. Common Stock",
        "NVIDIA Corporation Common Stock",
        "Arm Holdings plc American Depositary Shares",
        "Toronto Dominion Bank",
        "Northern Trust Corporation Common Stock",
        "Digital Realty Trust, Inc.",
        "State Street Corporation Common Stock",
    ]:
        assert not is_etf_like(name), name


def test_select_universe_excludes_etfs_when_named() -> None:
    stats = [
        SymbolStats(
            "AAPL", price=200.0, adv_dollar=90_000_000, name="Apple Inc. Common Stock"
        ),
        SymbolStats(
            "TQQQ", price=80.0, adv_dollar=99_000_000, name="ProShares UltraPro QQQ"
        ),
        SymbolStats(
            "SOXL",
            price=30.0,
            adv_dollar=99_000_000,
            name="Direxion Daily Semiconductor Bull 3X ETF",
        ),
    ]
    chosen = [s.symbol for s in select_universe(stats)]
    assert chosen == ["AAPL"]  # ETFs screened out despite higher ADV
