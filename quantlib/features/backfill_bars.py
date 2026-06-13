"""Alpaca historical minute bars → minute_agg frame, for ARBITRARY symbols (any of the ~10k US
equities) — the scalable backfill side of stream↔backfill correspondence.

Independent of any live capture: we can backfill exactly the symbols we collected live and verify
them. Isolated from the DB loaders so DB-only tooling stays Alpaca-free.
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl
from alpaca.data.enums import Adjustment
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

BARS_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
}
DAILY_SCHEMA = {"symbol": pl.String, "date": pl.Date, "close": pl.Float64}

_data_client: StockHistoricalDataClient | None = None


def _client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    return _data_client


def tradable_universe(limit: int | None = None) -> list[str]:
    """All active, tradable US common-equity symbols (the ~10k universe), sorted, optionally capped."""
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    assets = trading.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    symbols = sorted(a.symbol for a in assets if a.tradable and "/" not in a.symbol)
    return symbols[:limit] if limit else symbols


def backfill_bars(day: str, symbols: list[str], chunk: int = 200) -> pl.DataFrame:
    """Settled minute bars (symbol, minute, close, high, low) for `symbols` on `day`, full session."""
    start = dt.datetime.fromisoformat(f"{day}T00:00:00+00:00")
    end = dt.datetime.fromisoformat(f"{day}T23:59:59+00:00")
    rows = []
    for i in range(0, len(symbols), chunk):
        request = StockBarsRequest(
            symbol_or_symbols=symbols[i : i + chunk],
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            # RAW to MATCH the raw, unadjusted live tape — Adjustment.ALL back-adjusts every
            # historical price by dividend/split factors, so adjusted-backfill != raw-stream and
            # parity breaks by construction on any name with a corporate action (audit P0 #1).
            # Splits are handled explicitly at the feature layer via the corporate_actions table.
            adjustment=Adjustment.RAW,
        )
        barset = _client().get_stock_bars(request)
        for symbol, bars in barset.data.items():
            for bar in bars:
                rows.append((symbol, bar.timestamp, float(bar.close), float(bar.high), float(bar.low), float(bar.volume)))
    if not rows:
        return pl.DataFrame(schema=BARS_SCHEMA)
    return pl.DataFrame(rows, schema=["symbol", "minute", "close", "high", "low", "volume"], orient="row").cast(BARS_SCHEMA)


def backfill_daily(end_day: str, symbols: list[str], lookback_days: int = 45, chunk: int = 200) -> pl.DataFrame:
    """SPLIT-adjusted daily closes for the trailing ``lookback_days`` ending at ``end_day`` — the
    DAILY history cache for multi-day features. Split-adjusted (not raw) so multi-day returns are
    continuous across splits; this is a single shared artifact (no stream-vs-backfill divergence)."""
    end = dt.datetime.fromisoformat(f"{end_day}T23:59:59+00:00")
    start = end - dt.timedelta(days=lookback_days)
    rows = []
    for i in range(0, len(symbols), chunk):
        request = StockBarsRequest(
            symbol_or_symbols=symbols[i : i + chunk],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.SPLIT,
        )
        barset = _client().get_stock_bars(request)
        for symbol, bars in barset.data.items():
            for bar in bars:
                rows.append((symbol, bar.timestamp.date(), float(bar.close)))
    if not rows:
        return pl.DataFrame(schema=DAILY_SCHEMA)
    return pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row").cast(DAILY_SCHEMA)
