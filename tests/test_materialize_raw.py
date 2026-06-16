"""Unit + parity tests for ``load_raw_minute_agg`` — the MATERIALIZE-reads-/store/raw loader.

Network-free: we WRITE tiny raw bar partitions (the ``/store/raw`` schema) to a tmp dir and assert the
loader returns the ``backfill_bars`` shape. The parity test constructs a raw partition FROM a known
``backfill_bars``-shaped frame and asserts the loader reproduces that frame exactly (after sort) — the
segregation between ACQUIRE (raw download) and MATERIALIZE (raw → features) preserves the minute_agg
input cell-for-cell, so features cannot diverge by construction.
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl

from quantlib.data.raw_backfill import partition_dir
from quantlib.data.raw_fetchers import BARS_SCHEMA as RAW_BARS_SCHEMA
from quantlib.features.backfill_bars import BARS_SCHEMA
from quantlib.features.raw_loaders import load_raw_minute_agg

DAY = "2026-06-12"
TARGET_DAY = dt.date(2026, 6, 12)


def _write_raw_partition(store: str, symbol: str, day: dt.date, frame: pl.DataFrame) -> None:
    out_dir = partition_dir(store, "bars", symbol, day)
    os.makedirs(out_dir, exist_ok=True)
    frame.write_parquet(os.path.join(out_dir, "data.parquet"))


def _raw_frame(symbol: str, minutes: list[dt.datetime], rows: list[tuple]) -> pl.DataFrame:
    """Build a /store/raw bars-schema frame. Each row is (open, high, low, close, volume, vwap, tc)."""
    data = [
        (symbol, minute, *row)
        for minute, row in zip(minutes, rows)
    ]
    return pl.DataFrame(
        data,
        schema=["symbol", "ts", "open", "high", "low", "close", "volume", "vwap", "trade_count"],
        orient="row",
    ).cast(RAW_BARS_SCHEMA)


def test_load_raw_minute_agg_returns_minute_agg_shape(tmp_path) -> None:
    minute = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
    raw = _raw_frame("AAPL", [minute], [(10.0, 12.0, 9.0, 11.0, 500, 10.8, 7)])
    _write_raw_partition(str(tmp_path), "AAPL", TARGET_DAY, raw)

    frame = load_raw_minute_agg(str(tmp_path), DAY, ["AAPL"])

    assert frame.schema == BARS_SCHEMA
    assert frame.columns == ["symbol", "minute", "open", "close", "high", "low", "volume"]
    assert frame.height == 1
    record = frame.to_dicts()[0]
    assert record["symbol"] == "AAPL"
    assert record["minute"] == minute
    assert record["open"] == 10.0
    assert record["high"] == 12.0
    assert record["low"] == 9.0
    assert record["close"] == 11.0
    # raw volume is Int64; loader casts to Float64 to match backfill_bars
    assert record["volume"] == 500.0
    assert frame["volume"].dtype == pl.Float64


def test_missing_partition_symbol_is_absent(tmp_path) -> None:
    minute = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
    raw = _raw_frame("AAPL", [minute], [(10.0, 12.0, 9.0, 11.0, 500, 10.8, 7)])
    _write_raw_partition(str(tmp_path), "AAPL", TARGET_DAY, raw)

    # MSFT has no partition on disk -> absent (like Alpaca returning no rows), no error raised
    frame = load_raw_minute_agg(str(tmp_path), DAY, ["AAPL", "MSFT"])
    assert frame["symbol"].unique().to_list() == ["AAPL"]


def test_no_partitions_yields_typed_empty_frame(tmp_path) -> None:
    frame = load_raw_minute_agg(str(tmp_path), DAY, ["ZZZZ"])
    assert frame.height == 0
    assert frame.schema == BARS_SCHEMA


def test_parity_loader_reproduces_known_minute_agg(tmp_path) -> None:
    """Segregation parity: from a KNOWN backfill_bars-shaped frame, write the equivalent raw partitions
    and assert the loader reproduces that frame exactly (after sort). This is the network-free stand-in
    for ``load_raw_minute_agg(...) == backfill_bars(day, symbols)``."""
    minutes = [
        dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 12, 14, 31, tzinfo=dt.timezone.utc),
    ]
    expected = pl.DataFrame(
        [
            ("AAPL", minutes[0], 10.0, 11.0, 12.0, 9.0, 500.0),
            ("AAPL", minutes[1], 11.0, 11.5, 11.8, 10.9, 320.0),
            ("MSFT", minutes[0], 200.0, 201.0, 202.0, 199.0, 80.0),
        ],
        schema=["symbol", "minute", "open", "close", "high", "low", "volume"],
        orient="row",
    ).cast(BARS_SCHEMA)

    for symbol in ("AAPL", "MSFT"):
        sub = expected.filter(pl.col("symbol") == symbol)
        raw = pl.DataFrame(
            {
                "symbol": sub["symbol"],
                "ts": sub["minute"],
                "open": sub["open"],
                "high": sub["high"],
                "low": sub["low"],
                "close": sub["close"],
                "volume": sub["volume"].cast(pl.Int64),
                "vwap": sub["close"],
                "trade_count": pl.Series([1] * sub.height, dtype=pl.Int64),
            }
        ).cast(RAW_BARS_SCHEMA)
        _write_raw_partition(str(tmp_path), symbol, TARGET_DAY, raw)

    loaded = load_raw_minute_agg(str(tmp_path), DAY, ["AAPL", "MSFT"])

    sort_keys = ["symbol", "minute"]
    assert loaded.sort(sort_keys).equals(expected.sort(sort_keys))
