"""Parity tests for the direct-httpx columnar fast fetch path.

The columnar fast fetcher (``quantlib.data.fast_fetchers``) must produce frames cell-identical (after
sort) to the alpaca-py SDK fetchers (``quantlib.data.raw_fetchers``) for the same symbol-day. Two layers:

1. ``test_*_frame_parity`` — NETWORK-FREE: feed the SAME raw JSON rows to the columnar frame builder and
   to a SDK-shaped object list through the original ``_trade_row``/``_quote_row`` normalizer, assert the
   two frames are equal. This is the deterministic guard that runs in CI.
2. ``test_*_live_parity`` — opt-in (``RUN_LIVE_PARITY=1``): fetch a tiny LIGHT symbol-day live through
   BOTH paths and assert equality end-to-end (covers pagination + the real wire shape).
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl
import pytest
from alpaca.data.historical import StockHistoricalDataClient

from quantlib.data import fast_fetchers, raw_fetchers
from quantlib.data.raw_fetchers import QUOTES_SCHEMA, TRADES_SCHEMA

SYMBOL = "KO"
DAY = dt.date(2026, 6, 11)
EXT_HOURS_SYMBOL = "AAPL"
PRE_MARKET_CUTOFF = dt.datetime(2026, 6, 11, 13, 30, tzinfo=dt.timezone.utc)  # 09:30 ET
POST_MARKET_START = dt.datetime(2026, 6, 11, 20, 0, tzinfo=dt.timezone.utc)  # 16:00 ET

_RAW_TRADES: list[dict] = [
    # first rows have INTEGER size — a naive dict->frame would infer Int64 for `size` here ...
    {"t": "2026-06-11T08:00:00.027382133Z", "x": "P", "p": 83.34, "s": 18, "c": [" ", "T", "I"], "i": 52983525027890, "z": "A"},
    {"t": "2026-06-11T13:30:00.500000000Z", "x": "K", "p": 83.80, "s": 2, "c": ["@"], "i": 52983525027901, "z": "A"},
    # ... then a FRACTIONAL-share trade forces Float64 — the dtype that the schema demands and the SDK
    # produces. This row is the regression guard for the "found Float64 while building Int64" bug.
    {"t": "2026-06-11T16:00:00.123456789Z", "x": "D", "p": 83.95, "s": 585.8, "c": ["F", "I"], "i": 52983525027950, "z": "A"},
    {"t": "2026-06-11T20:00:00.999999999Z", "x": "N", "p": 84.10, "s": 100, "c": None, "i": 52983525027999, "z": "A"},
]

_RAW_QUOTES: list[dict] = [
    {"t": "2026-06-11T00:00:00.003961055Z", "bx": "P", "bp": 84.10, "bs": 100, "ax": "T", "ap": 84.28, "as": 100, "c": ["R"], "z": "A"},
    {"t": "2026-06-11T13:30:00.185187745Z", "bx": "P", "bp": 84.10, "bs": 200, "ax": "P", "ap": 84.32, "as": 300, "c": None, "z": "A"},
]


class _AttrRow:
    """Minimal stand-in for an alpaca-py trade/quote object (attribute access over a raw JSON row)."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self.__dict__.update(mapping)


def _sdk_trade_objects(rows: list[dict]) -> list[_AttrRow]:
    return [
        _AttrRow(
            {
                "timestamp": pl.Series([row["t"]]).str.to_datetime(time_unit="us", time_zone="UTC")[0],
                "price": row["p"],
                "size": row["s"],
                "exchange": row["x"],
                "conditions": row["c"],
                "tape": row["z"],
                "id": row["i"],
            }
        )
        for row in rows
    ]


def _sdk_quote_objects(rows: list[dict]) -> list[_AttrRow]:
    return [
        _AttrRow(
            {
                "timestamp": pl.Series([row["t"]]).str.to_datetime(time_unit="us", time_zone="UTC")[0],
                "bid_price": row["bp"],
                "bid_size": row["bs"],
                "bid_exchange": row["bx"],
                "ask_price": row["ap"],
                "ask_size": row["as"],
                "ask_exchange": row["ax"],
                "conditions": row["c"],
                "tape": row["z"],
            }
        )
        for row in rows
    ]


def _sdk_trades_frame(symbol: str, rows: list[dict]) -> pl.DataFrame:
    tuples = [raw_fetchers._trade_row(symbol, obj) for obj in _sdk_trade_objects(rows)]
    return pl.DataFrame(tuples, schema=list(TRADES_SCHEMA.keys()), orient="row").cast(TRADES_SCHEMA)


def _sdk_quotes_frame(symbol: str, rows: list[dict]) -> pl.DataFrame:
    tuples = [raw_fetchers._quote_row(symbol, obj) for obj in _sdk_quote_objects(rows)]
    return pl.DataFrame(tuples, schema=list(QUOTES_SCHEMA.keys()), orient="row").cast(QUOTES_SCHEMA)


def test_trades_frame_parity() -> None:
    fast_frame = fast_fetchers._trades_frame(SYMBOL, _RAW_TRADES)
    sdk_frame = _sdk_trades_frame(SYMBOL, _RAW_TRADES)
    assert fast_frame.schema == sdk_frame.schema
    assert fast_frame.sort("ts").equals(sdk_frame.sort("ts"))


def test_quotes_frame_parity() -> None:
    fast_frame = fast_fetchers._quotes_frame(SYMBOL, _RAW_QUOTES)
    sdk_frame = _sdk_quotes_frame(SYMBOL, _RAW_QUOTES)
    assert fast_frame.schema == sdk_frame.schema
    assert fast_frame.sort("ts").equals(sdk_frame.sort("ts"))


def test_empty_frames_match_schema() -> None:
    assert fast_fetchers._trades_frame(SYMBOL, []).schema == TRADES_SCHEMA
    assert fast_fetchers._quotes_frame(SYMBOL, []).schema == QUOTES_SCHEMA


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PARITY") != "1",
    reason="live parity hits Alpaca; set RUN_LIVE_PARITY=1 to run",
)
def test_trades_live_parity() -> None:
    sdk_client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    sdk_frame = raw_fetchers.fetch_trades_day(sdk_client, SYMBOL, DAY)
    with fast_fetchers.make_client() as fast_client:
        fast_frame = fast_fetchers.fetch_trades_day_fast(fast_client, SYMBOL, DAY)
    assert fast_frame.schema == sdk_frame.schema
    assert fast_frame.sort("ts", "trade_id").equals(sdk_frame.sort("ts", "trade_id"))


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PARITY") != "1",
    reason="live parity hits Alpaca; set RUN_LIVE_PARITY=1 to run",
)
def test_extended_hours_full_day_parity() -> None:
    """The fast path requests the FULL UTC day (00:00->24:00), feed=sip, NO session filter — so it must
    return ALL sessions: pre-market (ts < 09:30 ET), regular, AND post-market (ts >= 16:00 ET). Asserts
    the fast path's pre/reg/post split is non-empty in pre & post and that its TOTAL row count and split
    match the SDK path exactly for a liquid name. Features depend on extended-hours ticks, so a silent
    regular-session-only fetch would be a data-loss bug."""
    sdk_client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    sdk_frame = raw_fetchers.fetch_trades_day(sdk_client, EXT_HOURS_SYMBOL, DAY)
    with fast_fetchers.make_client() as fast_client:
        fast_frame = fast_fetchers.fetch_trades_day_fast(fast_client, EXT_HOURS_SYMBOL, DAY)

    def split(frame: pl.DataFrame) -> tuple[int, int, int]:
        pre = frame.filter(pl.col("ts") < PRE_MARKET_CUTOFF).height
        post = frame.filter(pl.col("ts") >= POST_MARKET_START).height
        reg = frame.height - pre - post
        return pre, reg, post

    fast_pre, fast_reg, fast_post = split(fast_frame)
    assert fast_pre > 0, "fast path returned no PRE-market trades — extended hours dropped"
    assert fast_post > 0, "fast path returned no POST-market trades — extended hours dropped"
    assert fast_reg > 0
    assert fast_frame.height == sdk_frame.height
    assert split(fast_frame) == split(sdk_frame)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PARITY") != "1",
    reason="live parity hits Alpaca; set RUN_LIVE_PARITY=1 to run",
)
def test_quotes_live_parity() -> None:
    sdk_client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    sdk_frame = raw_fetchers.fetch_quotes_day(sdk_client, SYMBOL, DAY)
    with fast_fetchers.make_client() as fast_client:
        fast_frame = fast_fetchers.fetch_quotes_day_fast(fast_client, SYMBOL, DAY)
    assert fast_frame.schema == sdk_frame.schema
    sort_cols = ["ts", "bid_price", "ask_price", "bid_size", "ask_size"]
    assert fast_frame.sort(sort_cols).equals(sdk_frame.sort(sort_cols))
