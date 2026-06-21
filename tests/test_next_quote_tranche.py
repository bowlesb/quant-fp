"""Unit tests for the #208 NEXT quote tranche computation (quantlib.data.next_quote_tranche).

The tranche is the liquid-head (top ADV band) names whose MEASURED median quoted spread falls in the tight
1-5bps band, ranked deepest-headroom first. These tests build a tmp store with controlled bars (so the
dollar-volume candidate ranking is deterministic) plus real quote partitions with controlled bid/ask (so the
spread/headroom measurement is deterministic), then assert: the spread/headroom math is correct, names
outside the 1-5bps band are excluded, settled-empty names are reported no-spread, and the final tranche is
the in-band candidates ordered by quoted headroom.
"""

from __future__ import annotations

import datetime as dt
import zlib

import polars as pl
import pytest

from quantlib.data import next_quote_tranche
from quantlib.data.raw_store import write_manifest_part, write_partition

DAY = dt.date(2026, 6, 12)


def _part_seq(tier: str, symbol: str) -> int:
    """A deterministic, per-(tier, symbol) manifest part_seq.

    Manifest part files are named ``part-{pid}-{part_seq}.parquet`` (raw_store.write_manifest_part), so two
    writes in the SAME process with the SAME part_seq collide and the second silently overwrites the first —
    dropping a symbol from the manifest. ``hash()`` is salted per-process by PYTHONHASHSEED, so a hash-derived
    seq makes that collision (and thus the drop) depend on the random seed of the worker process — flaky under
    ``pytest -n`` where each worker gets a fresh seed. crc32 is process-stable and 32-bit, so the handful of
    test symbols never collide and the seeding is identical on every run.
    """
    return zlib.crc32(f"{tier}:{symbol}".encode())


def _bars_frame(close: float, volume: int) -> pl.DataFrame:
    return pl.DataFrame({"close": [close], "volume": [volume]})


def _seed_bars(store: str, symbol: str, dollar_volume_rank_value: float) -> None:
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
        part_seq=_part_seq("bars", symbol),
    )


def _seed_quotes(
    store: str, symbol: str, bid: float, ask: float, bid_size: float, ask_size: float, n: int = 50
) -> None:
    """Write one real quote partition (n identical two-sided quotes) + its manifest row."""
    frame = pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "ts": [dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)] * n,
            "bid_price": [bid] * n,
            "bid_size": [bid_size] * n,
            "bid_exchange": ["Q"] * n,
            "ask_price": [ask] * n,
            "ask_size": [ask_size] * n,
            "ask_exchange": ["Q"] * n,
            "conditions": ["R"] * n,
            "tape": ["C"] * n,
        }
    )
    write_partition(store, "quotes", symbol, DAY, frame)
    write_manifest_part(
        store,
        "quotes",
        [
            {
                "tier": "quotes",
                "symbol": symbol,
                "date": DAY.isoformat(),
                "rows": n,
                "bytes": 100,
                "fetched_at": dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=_part_seq("quotes", symbol),
    )


def test_measure_spread_bps_and_headroom(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    # bid=99.99 ask=100.01 -> mid=100.0, spread=0.02 -> 2.0 bps; sizes 300/500 -> median headroom 400.
    _seed_quotes(store, "AAA", 99.99, 100.01, 300.0, 500.0)
    measured = next_quote_tranche.measure_spread_and_headroom(store, "AAA")
    assert measured is not None
    median_spread, median_size, n_quotes = measured
    assert median_spread == pytest.approx(2.0, abs=1e-6)
    assert median_size == pytest.approx(400.0)
    assert n_quotes == 50


def test_measure_returns_none_without_partitions(tmp_path: pytest.TempPathFactory) -> None:
    assert next_quote_tranche.measure_spread_and_headroom(str(tmp_path), "NOPE") is None


def test_crossed_and_glitch_quotes_excluded_from_median(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    # All-glitch: bid > ask (crossed) -> filtered out -> no sane rows -> None.
    _seed_quotes(store, "BAD", 100.05, 100.00, 100.0, 100.0)
    assert next_quote_tranche.measure_spread_and_headroom(store, "BAD") is None


def test_symbols_with_quotes_ignores_zero_row_markers(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    write_manifest_part(
        store,
        "quotes",
        [
            {
                "tier": "quotes",
                "symbol": "EMPTY",
                "date": DAY.isoformat(),
                "rows": 0,  # settled-empty marker — not real coverage
                "bytes": 0,
                "fetched_at": dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=1,
    )
    _seed_quotes(store, "REAL", 99.99, 100.01, 10.0, 10.0)
    assert next_quote_tranche.symbols_with_quotes(store) == {"REAL"}


def test_compute_tranche_filters_band_and_ranks_by_headroom(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = str(tmp_path)
    # Four liquid-head names, descending ADV liquidity TIGHT(2bps), WIDE(20bps), TIGHTER(3bps), and a
    # settled-empty name with no real tape.
    _seed_bars(store, "TIGHT", 4_000_000.0)
    _seed_bars(store, "WIDE", 3_000_000.0)
    _seed_bars(store, "TIGHTER", 2_000_000.0)
    _seed_bars(store, "EMPTY", 1_000_000.0)
    # TIGHT: 2 bps, headroom 100.  TIGHTER: 3 bps, headroom 900 (deeper).  WIDE: ~20 bps (out of band).
    _seed_quotes(store, "TIGHT", 99.99, 100.01, 100.0, 100.0)
    _seed_quotes(store, "TIGHTER", 99.985, 100.015, 900.0, 900.0)
    _seed_quotes(store, "WIDE", 99.90, 100.10, 100.0, 100.0)
    # EMPTY: a rows-0 manifest marker only (no real tape).
    write_manifest_part(
        store,
        "quotes",
        [
            {
                "tier": "quotes",
                "symbol": "EMPTY",
                "date": DAY.isoformat(),
                "rows": 0,
                "bytes": 0,
                "fetched_at": dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            }
        ],
        part_seq=999,
    )
    monkeypatch.setattr(next_quote_tranche, "HEAD_RANK_END", 4)
    monkeypatch.setattr(next_quote_tranche, "trading_client", lambda: object())
    monkeypatch.setattr(next_quote_tranche, "trading_days", lambda client, start, end: [DAY])

    ordered, diagnostics = next_quote_tranche.compute_next_tranche(store, DAY)

    # Tranche = the 1-5bps names, deepest-headroom first: TIGHTER (size 900) before TIGHT (size 100).
    assert ordered == ["TIGHTER", "TIGHT"]
    # WIDE is measured but out-of-band; EMPTY is a candidate with no sampled tape.
    wide_row = diagnostics.filter(pl.col("symbol") == "WIDE")
    assert not bool(wide_row["in_tranche"][0])
    assert wide_row["median_spread_bps"][0] == pytest.approx(20.0, abs=0.1)


def test_compute_tranche_empty_on_empty_store(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(next_quote_tranche, "trading_client", lambda: object())
    monkeypatch.setattr(next_quote_tranche, "trading_days", lambda client, start, end: [DAY])
    ordered, diagnostics = next_quote_tranche.compute_next_tranche(str(tmp_path), DAY)
    assert ordered == []
    assert diagnostics.height == 0
