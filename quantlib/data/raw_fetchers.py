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


def _range_bounds(start_day: dt.date, end_day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[start_day 00:00, end_day+1 00:00) UTC — inclusive day range, end-exclusive next-midnight.

    One request over this range returns EVERY day in [start_day, end_day]; the SDK pages through
    ``next_page_token`` internally, so a single call replaces one-request-per-day (the dominant cost
    of the 960k-request per-symbol-day backfill). The caller splits the multi-day frame back into the
    per-day partition layout.
    """
    start = dt.datetime(start_day.year, start_day.month, start_day.day, tzinfo=dt.timezone.utc)
    end = dt.datetime(end_day.year, end_day.month, end_day.day, tzinfo=dt.timezone.utc)
    return start, end + dt.timedelta(days=1)


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


def _bar_row(symbol: str, bar) -> tuple:  # type: ignore[no-untyped-def]
    return (
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


def _trade_row(symbol: str, trade) -> tuple:  # type: ignore[no-untyped-def]
    return (
        symbol,
        trade.timestamp,
        float(trade.price),
        float(trade.size),
        str(trade.exchange) if trade.exchange is not None else None,
        _join_conditions(trade.conditions),
        str(trade.tape) if trade.tape is not None else None,
        int(trade.id) if trade.id is not None else None,
    )


def _quote_row(symbol: str, quote) -> tuple:  # type: ignore[no-untyped-def]
    return (
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


def _frame(rows: list[tuple], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=list(schema.keys()), orient="row").cast(schema)


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
    return _frame([_bar_row(symbol, bar) for bar in barset.data.get(symbol, [])], BARS_SCHEMA)


def fetch_trades_day(
    client: StockHistoricalDataClient, symbol: str, day: dt.date
) -> pl.DataFrame:
    """RAW trades for one symbol over one UTC day, SIP feed, fully paginated by the SDK."""
    start, end = _day_bounds(day)
    request = StockTradesRequest(
        symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP
    )
    tradeset = _with_retry(f"trades {symbol} {day}", lambda: client.get_stock_trades(request))
    return _frame([_trade_row(symbol, trade) for trade in tradeset.data.get(symbol, [])], TRADES_SCHEMA)


def fetch_quotes_day(
    client: StockHistoricalDataClient, symbol: str, day: dt.date
) -> pl.DataFrame:
    """RAW NBBO quotes for one symbol over one UTC day, SIP feed, fully paginated by the SDK."""
    start, end = _day_bounds(day)
    request = StockQuotesRequest(
        symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP
    )
    quoteset = _with_retry(f"quotes {symbol} {day}", lambda: client.get_stock_quotes(request))
    return _frame([_quote_row(symbol, quote) for quote in quoteset.data.get(symbol, [])], QUOTES_SCHEMA)


def fetch_bars_multi(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start_day: dt.date,
    end_day: dt.date,
) -> dict[str, pl.DataFrame]:
    """RAW 1-minute bars for MANY symbols over a [start_day, end_day] range in ONE paginated request.

    Returns ``{symbol: frame}`` for every requested symbol (empty frame if Alpaca returned no bars).
    Rows are built with the SAME ``_bar_row`` normalizer as ``fetch_bars_day`` so a per-day slice of
    this output is cell-identical to fetching that day alone — the parity guarantee the backfill relies
    on. Bars are small, so multi-symbol batching is the big request-count win for the bars tier.
    """
    start, end = _range_bounds(start_day, end_day)
    request = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=DataFeed.SIP,
    )
    label = f"bars[{len(symbols)}] {start_day}..{end_day}"
    barset = _with_retry(label, lambda: client.get_stock_bars(request))
    return {
        symbol: _frame([_bar_row(symbol, bar) for bar in barset.data.get(symbol, [])], BARS_SCHEMA)
        for symbol in symbols
    }


def fetch_trades_range(
    client: StockHistoricalDataClient, symbol: str, start_day: dt.date, end_day: dt.date
) -> pl.DataFrame:
    """RAW trades for one symbol over a [start_day, end_day] range in ONE paginated request.

    Single-symbol (trade payloads are large — multi-symbol risks giant interleaved responses) but a
    bounded multi-day CHUNK collapses the per-day request count while keeping memory in check. Output
    is cell-identical to concatenating the per-day fetches (same ``_trade_row`` normalizer)."""
    start, end = _range_bounds(start_day, end_day)
    request = StockTradesRequest(symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP)
    label = f"trades {symbol} {start_day}..{end_day}"
    tradeset = _with_retry(label, lambda: client.get_stock_trades(request))
    return _frame([_trade_row(symbol, trade) for trade in tradeset.data.get(symbol, [])], TRADES_SCHEMA)


def fetch_quotes_range(
    client: StockHistoricalDataClient, symbol: str, start_day: dt.date, end_day: dt.date
) -> pl.DataFrame:
    """RAW NBBO quotes for one symbol over a [start_day, end_day] range in ONE paginated request.

    Single-symbol, small CHUNK (quotes are ~10-50x trade volume) — bounds memory while still cutting
    the per-day request count. Output is cell-identical to the per-day fetches (same ``_quote_row``)."""
    start, end = _range_bounds(start_day, end_day)
    request = StockQuotesRequest(symbol_or_symbols=symbol, start=start, end=end, feed=DataFeed.SIP)
    label = f"quotes {symbol} {start_day}..{end_day}"
    quoteset = _with_retry(label, lambda: client.get_stock_quotes(request))
    return _frame([_quote_row(symbol, quote) for quote in quoteset.data.get(symbol, [])], QUOTES_SCHEMA)
