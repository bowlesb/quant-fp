"""Unit tests for the shared research bar loader ``quantlib.research.load_raw_bars``.

Network-free: WRITE tiny ``/store/raw/bars`` partitions (the ``raw_fetchers.BARS_SCHEMA`` shape) to a
tmp dir and assert the loader returns FULL OHLCV — open/high/low as well as close/volume — so the
bar-SHAPE feature family (body/wick, gaps, intrabar range, candlestick patterns) can be invented on the
18-month raw tape. The pre-existing experiment loaders dropped open/high/low (close+volume only); this
shared loader is the additive substitute, exercised on production-shaped values.
"""

from __future__ import annotations

import datetime as dt
import os

import polars as pl

from quantlib.data.raw_fetchers import BARS_SCHEMA
from quantlib.data.raw_store import RAW_BAR_COLUMNS, load_raw_bars, partition_dir

DAY = "2026-06-12"
TARGET_DAY = dt.date(2026, 6, 12)
M0 = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)


def _write_bars(store: str, symbol: str, rows: list[tuple]) -> None:
    """rows: (ts, open, high, low, close, volume, vwap, trade_count)."""
    out_dir = partition_dir(store, "bars", symbol, TARGET_DAY)
    os.makedirs(out_dir, exist_ok=True)
    frame = pl.DataFrame(
        [(symbol, *row) for row in rows],
        schema=list(BARS_SCHEMA.keys()),
        orient="row",
    )
    frame = frame.cast(BARS_SCHEMA)  # type: ignore[arg-type]
    frame.write_parquet(os.path.join(out_dir, "data.parquet"))


def test_load_raw_bars_returns_full_ohlc(tmp_path) -> None:
    store = str(tmp_path)
    # one bar with all four OHLC corners DISTINCT so dropping any column would be detectable
    _write_bars(store, "AAA", [(M0, 10.0, 12.5, 9.5, 11.0, 1000, 11.2, 42)])

    bars = load_raw_bars(DAY, store=store)

    assert bars.columns == RAW_BAR_COLUMNS
    assert bars.height == 1
    row = bars.row(0, named=True)
    assert row["open"] == 10.0
    assert row["high"] == 12.5
    assert row["low"] == 9.5
    assert row["close"] == 11.0
    assert row["volume"] == 1000
    assert row["vwap"] == 11.2
    assert row["trade_count"] == 42


def test_load_raw_bars_spans_symbols_sorted_by_scan(tmp_path) -> None:
    store = str(tmp_path)
    _write_bars(store, "AAA", [(M0, 1.0, 2.0, 0.5, 1.5, 100, 1.4, 7)])
    _write_bars(store, "BBB", [(M0, 5.0, 6.0, 4.0, 5.5, 200, 5.4, 9)])

    bars = load_raw_bars(DAY, store=store)

    assert set(bars["symbol"].to_list()) == {"AAA", "BBB"}
    # high/low are real per-symbol values, not a close-derived fallback
    aaa = bars.filter(pl.col("symbol") == "AAA").row(0, named=True)
    assert (aaa["high"], aaa["low"]) == (2.0, 0.5)


def test_load_raw_bars_missing_day_is_empty_with_full_schema(tmp_path) -> None:
    bars = load_raw_bars("2099-01-01", store=str(tmp_path))
    assert bars.height == 0
    assert bars.columns == RAW_BAR_COLUMNS
