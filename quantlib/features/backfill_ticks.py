"""Alpaca historical tick backfill — the BACKFILL side of Layer-C parity.

Isolated from ``loaders`` (DB-only) so DB tooling never imports the Alpaca SDK. Normalizes Alpaca's
settled historical trades to the SAME (symbol, ts, price, size) shape and exchange timestamp as the
live loader — the one tick codepath that makes Layer-C parity meaningful (PARITY_PLAYBOOK §3).
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockTradesRequest

from quantlib.features.loaders import TICK_SCHEMA

_client_singleton: StockHistoricalDataClient | None = None


def _client() -> StockHistoricalDataClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = StockHistoricalDataClient(
            os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
        )
    return _client_singleton


def load_trades_backfill(start: dt.datetime, end: dt.datetime, symbols: list[str]) -> pl.DataFrame:
    request = StockTradesRequest(symbol_or_symbols=symbols, start=start, end=end)
    response = _client().get_stock_trades(request)
    rows = [
        (symbol, trade.timestamp, float(trade.price), float(trade.size))
        for symbol, trades in response.data.items()
        for trade in trades
    ]
    if not rows:
        return pl.DataFrame(schema=TICK_SCHEMA)
    return pl.DataFrame(rows, schema=["symbol", "ts", "price", "size"], orient="row").cast(TICK_SCHEMA)
