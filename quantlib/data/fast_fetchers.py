"""High-throughput RAW trades/quotes fetch — direct httpx + columnar polars (no per-row SDK objects).

The alpaca-py SDK builds one pydantic object PER TICK row (millions per mega-cap symbol-day) and that
parse holds the GIL, so a thread pool of "concurrent" SDK fetches really parses one-at-a-time. This
module bypasses the SDK: it hits the Alpaca market-data REST API directly with ``httpx``, parses each
JSON page's row LIST into polars by building COLUMN arrays (zero per-row Python objects beyond the dict
the JSON decoder already produced), and is designed to be driven by a PROCESS pool so download + parse
run truly in parallel beyond the GIL.

PARITY: ``fetch_trades_day_fast`` / ``fetch_quotes_day_fast`` emit frames whose schema, columns, dtypes
and cell values are identical (after sort) to ``raw_fetchers.fetch_trades_day`` / ``fetch_quotes_day``
for the same symbol-day. Timestamps parse with ``str.to_datetime(time_unit="us")`` which TRUNCATES the
RFC3339 nanosecond field exactly as the SDK does (``...027382133Z`` -> ``27382`` micros); ``conditions``
is the same comma-joined string; integer ``size`` is cast to Float64 to match the schema.

Endpoints (feed=sip, full tape):
    GET https://data.alpaca.markets/v2/stocks/trades?symbols=<SYM>&start=..&end=..&limit=10000&feed=sip
    GET https://data.alpaca.markets/v2/stocks/quotes?...
Auth headers ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` from env ``ALPACA_KEY_ID`` / ``ALPACA_SECRET_KEY``.
Trade row:  {t,x,p,s,c,i,z}   Quote row: {t,bx,bp,bs,ax,ap,as,c,z}
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

import httpx
import polars as pl

from quantlib.data.raw_fetchers import QUOTES_SCHEMA, TRADES_SCHEMA, _join_conditions

logger = logging.getLogger("fast_fetchers")

DATA_BASE_URL = "https://data.alpaca.markets/v2/stocks"
PAGE_LIMIT = 10000
FEED = "sip"
REQUEST_TIMEOUT_SECONDS = 90.0
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
HTTP_POOL_SIZE = 32


def _auth_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        "Accept": "application/json",
    }


def make_client() -> httpx.Client:
    """An httpx client with a large connection pool and keep-alive, reusable across many symbol-days."""
    limits = httpx.Limits(
        max_connections=HTTP_POOL_SIZE,
        max_keepalive_connections=HTTP_POOL_SIZE,
        keepalive_expiry=30.0,
    )
    return httpx.Client(
        headers=_auth_headers(), timeout=REQUEST_TIMEOUT_SECONDS, limits=limits
    )


def _day_bounds(day: dt.date) -> tuple[str, str]:
    """[00:00, 24:00) UTC bounds for a single calendar day as RFC3339 strings (end-exclusive midnight)."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _get_page(
    client: httpx.Client, endpoint: str, params: dict[str, str | int]
) -> dict:
    """One GET with bounded exponential back-off on 429 / 5xx. Other statuses raise immediately."""
    attempt = 0
    while True:
        response = client.get(f"{DATA_BASE_URL}/{endpoint}", params=params)
        if response.status_code == 200:
            return json.loads(response.content)
        if response.status_code == 429 or 500 <= response.status_code < 600:
            attempt += 1
            if attempt > MAX_RETRIES:
                response.raise_for_status()
            sleep_seconds = min(
                BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS
            )
            logger.warning(
                "%s %s: HTTP %d (attempt %d/%d), backing off %.1fs",
                endpoint,
                params.get("symbols"),
                response.status_code,
                attempt,
                MAX_RETRIES,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
            continue
        response.raise_for_status()


def _paginate(
    client: httpx.Client, endpoint: str, symbol: str, day: dt.date
) -> list[dict]:
    """Collect every row for one symbol-day, following ``next_page_token`` to exhaustion."""
    start, end = _day_bounds(day)
    params: dict[str, str | int] = {
        "symbols": symbol,
        "start": start,
        "end": end,
        "limit": PAGE_LIMIT,
        "feed": FEED,
    }
    rows: list[dict] = []
    key = "trades" if endpoint == "trades" else "quotes"
    while True:
        payload = _get_page(client, endpoint, params)
        page_rows = payload[key].get(symbol)
        if page_rows:
            rows.extend(page_rows)
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return rows


def _column(name: str, values: list, dtype: pl.DataType) -> pl.Series:
    """Build a Series with the SCHEMA's target dtype directly (``strict=False`` so an int JSON value lands
    in a Float64 column and a float-typed id is coerced to Int64) — never infer-then-cast, which can FAIL
    mid-frame when a column's first values imply a narrower dtype than a later row needs."""
    return pl.Series(name, values, dtype=dtype, strict=False)


def _trades_frame(symbol: str, rows: list[dict]) -> pl.DataFrame:
    """Build the TRADES_SCHEMA frame from raw JSON rows by COLUMN (no per-row Python tuple objects)."""
    if not rows:
        return pl.DataFrame(schema=TRADES_SCHEMA)
    ts = _column("ts", [row["t"] for row in rows], pl.String).str.to_datetime(
        time_unit="us", time_zone="UTC"
    )
    return pl.DataFrame(
        [
            _column("symbol", [symbol] * len(rows), pl.String),
            ts,
            _column("price", [row["p"] for row in rows], pl.Float64),
            _column("size", [row["s"] for row in rows], pl.Float64),
            _column("exchange", [row.get("x") for row in rows], pl.String),
            _column("conditions", [_join_conditions(row.get("c")) for row in rows], pl.String),
            _column("tape", [row.get("z") for row in rows], pl.String),
            _column("trade_id", [row.get("i") for row in rows], pl.Int64),
        ]
    )


def _quotes_frame(symbol: str, rows: list[dict]) -> pl.DataFrame:
    """Build the QUOTES_SCHEMA frame from raw JSON rows by COLUMN."""
    if not rows:
        return pl.DataFrame(schema=QUOTES_SCHEMA)
    ts = _column("ts", [row["t"] for row in rows], pl.String).str.to_datetime(
        time_unit="us", time_zone="UTC"
    )
    return pl.DataFrame(
        [
            _column("symbol", [symbol] * len(rows), pl.String),
            ts,
            _column("bid_price", [row["bp"] for row in rows], pl.Float64),
            _column("bid_size", [row["bs"] for row in rows], pl.Float64),
            _column("bid_exchange", [row.get("bx") for row in rows], pl.String),
            _column("ask_price", [row["ap"] for row in rows], pl.Float64),
            _column("ask_size", [row["as"] for row in rows], pl.Float64),
            _column("ask_exchange", [row.get("ax") for row in rows], pl.String),
            _column("conditions", [_join_conditions(row.get("c")) for row in rows], pl.String),
            _column("tape", [row.get("z") for row in rows], pl.String),
        ]
    )


def fetch_trades_day_fast(client: httpx.Client, symbol: str, day: dt.date) -> pl.DataFrame:
    """RAW trades for one symbol over one UTC day, SIP feed — cell-identical to fetch_trades_day."""
    return _trades_frame(symbol, _paginate(client, "trades", symbol, day))


def fetch_quotes_day_fast(client: httpx.Client, symbol: str, day: dt.date) -> pl.DataFrame:
    """RAW NBBO quotes for one symbol over one UTC day, SIP feed — cell-identical to fetch_quotes_day."""
    return _quotes_frame(symbol, _paginate(client, "quotes", symbol, day))
