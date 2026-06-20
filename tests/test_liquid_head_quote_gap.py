"""Unit tests for the liquid-head zero-quote set computation (quantlib.data.liquid_head_quote_gap).

The fill target is the most-liquid (ADV rank < head_rank) names that have bars but NO quote tape — in
production chiefly the SPDR sector ETFs. These tests build a tmp store with controlled bars (so the
dollar-volume ranking is deterministic) plus a quotes manifest that already covers SOME names, then
assert the computed set is exactly the liquid head minus the already-quoted names, most-liquid first.
The Alpaca-dependent ranking-window helpers are exercised directly (rank_by_dollar_volume reads on-disk
bars, no network); the full compute_liquid_head_zero_quote_set is driven with the head cutoff narrowed
so a small synthetic universe lands in-head.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.data import liquid_head_quote_gap
from quantlib.data.raw_backfill import rank_by_dollar_volume
from quantlib.data.raw_store import write_manifest_part, write_partition

DAY = dt.date(2026, 6, 12)


def _bars_frame(close: float, volume: int) -> pl.DataFrame:
    return pl.DataFrame({"close": [close], "volume": [volume]})


def _seed_bars(store: str, symbol: str, dollar_volume_rank_value: float) -> None:
    """Write one bars partition + its manifest row so the symbol is in the rankable universe.

    close*volume == dollar_volume_rank_value drives the ADV rank (higher == more liquid == lower rank)."""
    write_partition(store, "bars", symbol, DAY, _bars_frame(dollar_volume_rank_value, 1))
    write_manifest_part(
        store,
        "bars",
        [
            {
                "tier": "bars",
                "symbol": symbol,
                "date": DAY.isoformat(),
                "rows": 1,
                "bytes": 1,
                "fetched_at": dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=hash(symbol) % 1000,
    )


def _seed_quote_coverage(store: str, symbol: str) -> None:
    write_manifest_part(
        store,
        "quotes",
        [
            {
                "tier": "quotes",
                "symbol": symbol,
                "date": DAY.isoformat(),
                "rows": 100,
                "bytes": 100,
                "fetched_at": dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=hash(symbol) % 1000,
    )


def test_bars_universe_excludes_market_tickers(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_bars(store, "AAA", 1000.0)
    _seed_bars(store, "SPY", 9999.0)  # market ticker — must be excluded
    universe = liquid_head_quote_gap.bars_universe(store)
    assert "AAA" in universe
    assert "SPY" not in universe


def test_symbols_with_quotes_reads_manifest(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_quote_coverage(store, "AAA")
    assert liquid_head_quote_gap.symbols_with_quotes(store) == {"AAA"}


def test_symbols_with_quotes_empty_on_empty_store(
    tmp_path: pytest.TempPathFactory,
) -> None:
    assert liquid_head_quote_gap.symbols_with_quotes(str(tmp_path)) == set()


def test_ranker_orders_by_dollar_volume(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_bars(store, "HI", 1_000_000.0)
    _seed_bars(store, "MID", 1_000.0)
    _seed_bars(store, "LO", 1.0)
    ranked = rank_by_dollar_volume(store, ["LO", "MID", "HI"], [DAY], sample_days=0)
    assert ranked == ["HI", "MID", "LO"]


def test_compute_head_set_is_head_minus_already_quoted(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    # Six symbols, descending liquidity: rank order S0(most) .. S5(least).
    for idx in range(6):
        _seed_bars(store, f"S{idx}", float(10**6 // (idx + 1)))
    # Head cutoff = top 4 -> {S0, S1, S2, S3}.
    # S1 already has quotes -> must be dropped; result = [S0, S2, S3] (most-liquid first).
    _seed_quote_coverage(store, "S1")
    # Avoid the Alpaca calendar: feed a fixed trading-day list straight to the ranker.
    monkeypatch.setattr(liquid_head_quote_gap, "trading_client", lambda: object())
    monkeypatch.setattr(liquid_head_quote_gap, "trading_days", lambda client, start, end: [DAY])

    targets = liquid_head_quote_gap.compute_liquid_head_zero_quote_set(store, DAY, head_rank=4)
    assert targets == ["S0", "S2", "S3"]


def test_compute_head_set_respects_head_rank_cutoff(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    for idx in range(6):
        _seed_bars(store, f"S{idx}", float(10**6 // (idx + 1)))
    monkeypatch.setattr(liquid_head_quote_gap, "trading_client", lambda: object())
    monkeypatch.setattr(liquid_head_quote_gap, "trading_days", lambda client, start, end: [DAY])
    # head_rank=2 -> only the top two names are candidates; none quoted -> [S0, S1].
    targets = liquid_head_quote_gap.compute_liquid_head_zero_quote_set(store, DAY, head_rank=2)
    assert targets == ["S0", "S1"]


def test_compute_head_set_empty_when_head_fully_quoted(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    for idx in range(6):
        _seed_bars(store, f"S{idx}", float(10**6 // (idx + 1)))
        _seed_quote_coverage(store, f"S{idx}")  # every name already has quotes
    monkeypatch.setattr(liquid_head_quote_gap, "trading_client", lambda: object())
    monkeypatch.setattr(liquid_head_quote_gap, "trading_days", lambda client, start, end: [DAY])
    assert liquid_head_quote_gap.compute_liquid_head_zero_quote_set(store, DAY, head_rank=6) == []


def test_compute_head_set_empty_on_empty_store(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liquid_head_quote_gap, "trading_client", lambda: object())
    monkeypatch.setattr(liquid_head_quote_gap, "trading_days", lambda client, start, end: [DAY])
    assert liquid_head_quote_gap.compute_liquid_head_zero_quote_set(str(tmp_path), DAY) == []
