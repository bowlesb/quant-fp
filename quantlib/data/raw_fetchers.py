"""Historical RAW market-data fetchers for the shared `/store/raw/` dataset.

Full-fidelity, paginated fetch of minute BARS, raw TRADES and raw QUOTES from Alpaca for a single
symbol over a single UTC day, normalized into polars frames ready to write as parquet. This is the
RAW substrate the modelling agent reads (PARITY is much less of a concern than for computed features):
we keep every microstructure field Alpaca returns (exchange / conditions / tape / id) rather than the
narrow (symbol, ts, price, size) live-parity shape used by the feature loaders.

Pagination is handled INSIDE the alpaca-py SDK: ``get_stock_trades`` / ``get_stock_quotes`` page through
``next_page_token`` until the window is exhausted when no ``limit`` is set (page size 10k). We add a thin
retry/back-off wrapper so a transient 429/5xx on a multi-page symbol-day does not abort the backfill.

Feed is SIP (full tape). ``adjustment`` does not apply to ticks; bars are fetched RAW (unadjusted) so the
stored data is the literal tape — split/dividend handling is the reader's choice, not baked in here.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import polars as pl
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger("raw_fetchers")

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

BARS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "vwap": pl.Float64,
    "trade_count": pl.Int64,
}

TRADES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "price": pl.Float64,
    "size": pl.Float64,
    "exchange": pl.String,
    "conditions": pl.String,
    "tape": pl.String,
    "trade_id": pl.Int64,
}

QUOTES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "bid_price": pl.Float64,
    "bid_size": pl.Float64,
    "bid_exchange": pl.String,
    "ask_price": pl.Float64,
    "ask_size": pl.Float64,
    "ask_exchange": pl.String,
    "conditions": pl.String,
    "tape": pl.String,
}


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[00:00, 24:00) UTC bounds for a single calendar day (end-exclusive next-midnight)."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def _join_conditions(conditions: list[str] | None) -> str | None:
    """Alpaca tick `conditions` is a list of single-char codes; store as a comma-joined string."""
    if not conditions:
        return None
    return ",".join(str(condition) for condition in conditions)


def _with_retry(call_label: str, fetch):  # type: ignore[no-untyped-def]
    """Call `fetch()` with bounded exponential back-off on Alpaca rate-limit / transient API errors.

    Only retries APIError (covers 429 + 5xx). Any other exception propagates — we want real bugs loud.
    """
    attempt = 0
    while True:
        try:
            return fetch()
        except APIError as error:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise
            sleep_seconds = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
            logger.warning(
                "%s: API error (attempt %d/%d), backing off %.1fs: %s",
                call_label,
                attempt,
                MAX_RETRIES,
                sleep_seconds,
                error,
            )
            time.sleep(sleep_seconds)


def fetch_bars_day(
    client: StockHistoricalDataClient, symbol: str, day: dt.date
) -> pl.DataFrame:
    """RAW (unadjusted) 1-minute bars for one symbol over one UTC day, SIP feed."""
    start, end = _day_bounds(day)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=DataFeed.SIP,
    )
    barset = _with_retry(f"bars {symbol} {day}", lambda: client.get_stock_bars(request))
    bars = barset.data.get(symbol, [])
    rows = [
        (
            symbol,
            bar.timestamp,
            float(bar.open),
            float(bar.high),
            float(bar.low),
            float(bar.close),
            int(bar.volume),
            float(bar.vwap) if bar.vwap is not None else None,
            int(bar.trade_count) if bar.trade_count is not None else None,
        )
        for bar in bars
    ]
    if not rows:
        return pl.DataFrame(schema=BARS_SCHEMA)
    return pl.DataFrame(rows, schema=list(BARS_SCHEMA.keys()), orient="row").cast(BARS_SCHEMA)


def fetch_trades_day(
    client: StockHistoricalDataClient, symbol: str, day: dt.date
) -> pl.DataFrame:
    """RAW trades for one symbol over one UTC day, SIP feed, fully paginated by the SDK."""
    start, end = _day_bounds(day)
    request = StockTradesRequest(
        symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP
    )
    tradeset = _with_retry(f"trades {symbol} {day}", lambda: client.get_stock_trades(request))
    trades = tradeset.data.get(symbol, [])
    rows = [
        (
            symbol,
            trade.timestamp,
            float(trade.price),
            float(trade.size),
            str(trade.exchange) if trade.exchange is not None else None,
            _join_conditions(trade.conditions),
            str(trade.tape) if trade.tape is not None else None,
            int(trade.id) if trade.id is not None else None,
        )
        for trade in trades
    ]
    if not rows:
        return pl.DataFrame(schema=TRADES_SCHEMA)
    return pl.DataFrame(rows, schema=list(TRADES_SCHEMA.keys()), orient="row").cast(TRADES_SCHEMA)


def fetch_quotes_day(
    client: StockHistoricalDataClient, symbol: str, day: dt.date
) -> pl.DataFrame:
    """RAW NBBO quotes for one symbol over one UTC day, SIP feed, fully paginated by the SDK."""
    start, end = _day_bounds(day)
    request = StockQuotesRequest(
        symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP
    )
    quoteset = _with_retry(f"quotes {symbol} {day}", lambda: client.get_stock_quotes(request))
    quotes = quoteset.data.get(symbol, [])
    rows = [
        (
            symbol,
            quote.timestamp,
            float(quote.bid_price),
            float(quote.bid_size),
            str(quote.bid_exchange) if quote.bid_exchange is not None else None,
            float(quote.ask_price),
            float(quote.ask_size),
            str(quote.ask_exchange) if quote.ask_exchange is not None else None,
            _join_conditions(quote.conditions),
            str(quote.tape) if quote.tape is not None else None,
        )
        for quote in quotes
    ]
    if not rows:
        return pl.DataFrame(schema=QUOTES_SCHEMA)
    return pl.DataFrame(rows, schema=list(QUOTES_SCHEMA.keys()), orient="row").cast(QUOTES_SCHEMA)
