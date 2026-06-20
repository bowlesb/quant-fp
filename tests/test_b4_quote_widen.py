"""Unit tests for the #208 B4 quote-widening set computation (quantlib.data.b4_quote_widen).

The widening target is the ADV-rank-2000-4000 (B4) names that have bars but NO quote tape. These tests
build a tmp store with controlled bars (so the dollar-volume ranking is deterministic) plus a quotes
manifest that already covers SOME names, then assert the computed set is exactly the B4 band minus the
already-quoted names, most-liquid first. The Alpaca-dependent ranking-window helpers are exercised
directly (rank_by_dollar_volume reads on-disk bars, no network); the full compute_b4_zero_quote_set is
driven with the band bounds narrowed via monkeypatch so a small synthetic universe lands in-band.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.data import b4_quote_widen
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
    universe = b4_quote_widen.bars_universe(store)
    assert "AAA" in universe
    assert "SPY" not in universe


def test_symbols_with_quotes_reads_manifest(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_quote_coverage(store, "AAA")
    assert b4_quote_widen.symbols_with_quotes(store) == {"AAA"}


def test_symbols_with_quotes_empty_on_empty_store(
    tmp_path: pytest.TempPathFactory,
) -> None:
    assert b4_quote_widen.symbols_with_quotes(str(tmp_path)) == set()


def test_ranker_orders_by_dollar_volume(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    _seed_bars(store, "HI", 1_000_000.0)
    _seed_bars(store, "MID", 1_000.0)
    _seed_bars(store, "LO", 1.0)
    ranked = rank_by_dollar_volume(store, ["LO", "MID", "HI"], [DAY], sample_days=0)
    assert ranked == ["HI", "MID", "LO"]


def test_compute_b4_set_is_band_minus_already_quoted(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    # Six symbols, descending liquidity: rank order S0(most) .. S5(least).
    for idx in range(6):
        _seed_bars(store, f"S{idx}", float(10**6 // (idx + 1)))
    # Pretend the B4 band is ranks [2:5) of this tiny universe -> {S2, S3, S4}.
    monkeypatch.setattr(b4_quote_widen, "B4_RANK_START", 2)
    monkeypatch.setattr(b4_quote_widen, "B4_RANK_END", 5)
    # S3 already has quotes -> must be dropped; result = [S2, S4] (most-liquid first).
    _seed_quote_coverage(store, "S3")
    # Avoid the Alpaca calendar: feed a fixed trading-day list straight to the ranker.
    monkeypatch.setattr(b4_quote_widen, "trading_client", lambda: object())
    monkeypatch.setattr(b4_quote_widen, "trading_days", lambda client, start, end: [DAY])

    targets = b4_quote_widen.compute_b4_zero_quote_set(store, DAY)
    assert targets == ["S2", "S4"]


def test_compute_b4_set_empty_when_band_fully_quoted(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    for idx in range(6):
        _seed_bars(store, f"S{idx}", float(10**6 // (idx + 1)))
        _seed_quote_coverage(store, f"S{idx}")  # every name already has quotes
    monkeypatch.setattr(b4_quote_widen, "B4_RANK_START", 2)
    monkeypatch.setattr(b4_quote_widen, "B4_RANK_END", 5)
    monkeypatch.setattr(b4_quote_widen, "trading_client", lambda: object())
    monkeypatch.setattr(b4_quote_widen, "trading_days", lambda client, start, end: [DAY])
    assert b4_quote_widen.compute_b4_zero_quote_set(store, DAY) == []


def test_compute_b4_set_empty_on_empty_store(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(b4_quote_widen, "trading_client", lambda: object())
    monkeypatch.setattr(b4_quote_widen, "trading_days", lambda client, start, end: [DAY])
    assert b4_quote_widen.compute_b4_zero_quote_set(str(tmp_path), DAY) == []
