"""Unit tests for the raw TICK loaders — ``load_raw_tick_enriched_minute_agg`` + ``load_raw_trades``.

Network-free: WRITE tiny ``/store/raw/trades`` + ``/store/raw/quotes`` partitions to a tmp dir and assert
the loaders produce the per-minute tick columns the order-flow groups (trade_flow / quote_spread /
liquidity / signed_trade_ratio) and the per-trade ``trades`` frame (tick_runlength / microstructure_burst)
declare as inputs — the frames the durable backfill materialize must supply so those groups become
runnable and get a backfill side to validate against.
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl

from quantlib.data.raw_backfill import partition_dir
from quantlib.features.raw_loaders import (
    TRADES_SCHEMA,
    load_raw_tick_enriched_minute_agg,
    load_raw_trades,
)

DAY = "2026-06-12"
TARGET_DAY = dt.date(2026, 6, 12)
M0 = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)


def _write(store: str, tier: str, symbol: str, frame: pl.DataFrame) -> None:
    out_dir = partition_dir(store, tier, symbol, TARGET_DAY)
    os.makedirs(out_dir, exist_ok=True)
    frame.write_parquet(os.path.join(out_dir, "data.parquet"))


def _trades(symbol: str, rows: list[tuple]) -> pl.DataFrame:
    """rows: (ts, price, size)."""
    return pl.DataFrame(
        [(symbol, *row) for row in rows], schema=["symbol", "ts", "price", "size"], orient="row"
    ).cast(TRADES_SCHEMA)


def _quotes(symbol: str, rows: list[tuple]) -> pl.DataFrame:
    """rows: (ts, bid_price, ask_price, bid_size, ask_size)."""
    return pl.DataFrame(
        [(symbol, *row) for row in rows],
        schema=["symbol", "ts", "bid_price", "ask_price", "bid_size", "ask_size"],
        orient="row",
    )


def _bars(symbol: str, minute: dt.datetime, close: float) -> pl.DataFrame:
    return pl.DataFrame(
        [(symbol, minute, close, close, close, close, 100.0)],
        schema=["symbol", "minute", "open", "close", "high", "low", "volume"],
        orient="row",
    )


def test_tick_enriched_adds_trade_and_quote_columns(tmp_path) -> None:
    # three trades in one minute: up, up, down -> signs +1,+1(carried up),-1
    _write(
        str(tmp_path), "trades", "AAPL",
        _trades("AAPL", [(M0, 10.0, 100.0), (M0 + dt.timedelta(seconds=1), 11.0, 200.0),
                          (M0 + dt.timedelta(seconds=2), 10.5, 50.0)]),
    )
    _write(
        str(tmp_path), "quotes", "AAPL",
        _quotes("AAPL", [(M0, 10.0, 10.2, 300.0, 100.0)]),
    )
    bars = _bars("AAPL", M0, 10.5)

    enriched = load_raw_tick_enriched_minute_agg(str(tmp_path), DAY, ["AAPL"], bars)

    for col in ("n_trades", "signed_volume", "mean_spread_bps", "quote_imbalance",
                "mean_bid_size", "mean_ask_size"):
        assert col in enriched.columns, col
    record = enriched.filter(pl.col("minute") == M0).to_dicts()[0]
    assert record["n_trades"] == 3.0
    # first trade has no prior -> sign defaults +1; +100; uptick +200; downtick -50 -> 100+200-50 = 250
    assert record["signed_volume"] == 250.0
    # spread = (10.2-10.0)/10.1*1e4 ~= 198.02 bps ; imbalance = (300-100)/400 = 0.5
    assert abs(record["mean_spread_bps"] - 198.0198) < 0.01
    assert abs(record["quote_imbalance"] - 0.5) < 1e-9
    assert record["mean_bid_size"] == 300.0
    assert record["mean_ask_size"] == 100.0


def test_tick_enriched_left_joins_no_tick_symbol_as_null(tmp_path) -> None:
    """A symbol with bars but NO raw trades keeps its bar row with null tick columns (honest sparsity)."""
    _write(str(tmp_path), "trades", "AAPL", _trades("AAPL", [(M0, 10.0, 100.0)]))
    bars = pl.concat([_bars("AAPL", M0, 10.0), _bars("MSFT", M0, 200.0)])

    enriched = load_raw_tick_enriched_minute_agg(str(tmp_path), DAY, ["AAPL", "MSFT"], bars)

    assert enriched.height == 2
    msft = enriched.filter(pl.col("symbol") == "MSFT").to_dicts()[0]
    assert msft["n_trades"] is None
    assert msft["signed_volume"] is None


def test_tick_enriched_no_trades_returns_bars_unchanged(tmp_path) -> None:
    bars = _bars("ZZZZ", M0, 1.0)
    enriched = load_raw_tick_enriched_minute_agg(str(tmp_path), DAY, ["ZZZZ"], bars)
    assert enriched.equals(bars)


def test_load_raw_trades_shape_and_sort(tmp_path) -> None:
    _write(str(tmp_path), "trades", "AAPL",
           _trades("AAPL", [(M0 + dt.timedelta(seconds=2), 10.5, 50.0), (M0, 10.0, 100.0)]))
    frame = load_raw_trades(str(tmp_path), DAY, ["AAPL", "MSFT"])
    assert frame.columns == ["symbol", "ts", "price", "size"]
    assert frame.height == 2
    # sorted by (symbol, ts)
    assert frame["ts"].to_list() == sorted(frame["ts"].to_list())


def test_load_raw_trades_empty_is_typed(tmp_path) -> None:
    frame = load_raw_trades(str(tmp_path), DAY, ["ZZZZ"])
    assert frame.height == 0
    assert frame.schema == TRADES_SCHEMA
