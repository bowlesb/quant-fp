"""Unit tests for the shared /store/raw bars/trades/quotes backfill (fetchers + orchestrator).

No network: Alpaca clients are mocked at module level. Covers fetcher normalization/schema, the
SIP-feed + raw-adjustment request shape, manifest resume (skip-done), the budget/headroom STOP, and
dollar-volume ranking from on-disk bars.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

import polars as pl
import pytest

from quantlib.data import raw_backfill
from quantlib.data.raw_fetchers import (
    BARS_SCHEMA,
    QUOTES_SCHEMA,
    TRADES_SCHEMA,
    fetch_bars_day,
    fetch_quotes_day,
    fetch_trades_day,
)

DAY = dt.date(2026, 6, 12)


@dataclass
class FakeBar:
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    trade_count: int


@dataclass
class FakeTrade:
    timestamp: dt.datetime
    price: float
    size: float
    exchange: str
    conditions: list[str]
    tape: str
    id: int


@dataclass
class FakeQuote:
    timestamp: dt.datetime
    bid_price: float
    bid_size: float
    bid_exchange: str
    ask_price: float
    ask_size: float
    ask_exchange: str
    conditions: list[str]
    tape: str


class FakeSet:
    def __init__(self, data: dict) -> None:
        self.data = data


class MockDataClient:
    """Records the last request so tests can assert feed=SIP / adjustment=RAW shape."""

    def __init__(self) -> None:
        self.last_request = None

    def get_stock_bars(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet({"AAPL": [FakeBar(ts, 1.0, 2.0, 0.5, 1.5, 100, 1.4, 9)]})

    def get_stock_trades(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet(
            {"AAPL": [FakeTrade(ts, 1.5, 100.0, "Q", ["@", "I"], "C", 42)]}
        )

    def get_stock_quotes(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet(
            {"AAPL": [FakeQuote(ts, 1.4, 10.0, "P", 1.6, 12.0, "Q", ["R"], "C")]}
        )


def test_fetch_bars_schema_and_request_shape() -> None:
    client = MockDataClient()
    frame = fetch_bars_day(client, "AAPL", DAY)
    assert frame.schema == BARS_SCHEMA
    assert frame.height == 1
    assert frame["close"][0] == 1.5
    assert client.last_request.feed.value == "sip"
    assert client.last_request.adjustment.value == "raw"


def test_fetch_trades_keeps_microstructure() -> None:
    client = MockDataClient()
    frame = fetch_trades_day(client, "AAPL", DAY)
    assert frame.schema == TRADES_SCHEMA
    assert frame["conditions"][0] == "@,I"
    assert frame["exchange"][0] == "Q"
    assert frame["trade_id"][0] == 42
    assert client.last_request.feed.value == "sip"


def test_fetch_quotes_keeps_nbbo() -> None:
    client = MockDataClient()
    frame = fetch_quotes_day(client, "AAPL", DAY)
    assert frame.schema == QUOTES_SCHEMA
    assert frame["bid_price"][0] == 1.4
    assert frame["ask_size"][0] == 12.0
    assert client.last_request.feed.value == "sip"


def test_empty_response_yields_typed_empty_frame() -> None:
    class EmptyClient(MockDataClient):
        def get_stock_trades(self, request):  # type: ignore[no-untyped-def]
            return FakeSet({})

    frame = fetch_trades_day(EmptyClient(), "ZZZZ", DAY)
    assert frame.height == 0
    assert frame.schema == TRADES_SCHEMA


def _config(tmp_path, budget_bytes: int, max_workers: int = 1) -> raw_backfill.BackfillConfig:
    return raw_backfill.BackfillConfig(
        store=str(tmp_path),
        months=6,
        top_trades=2,
        top_quotes=1,
        budget_bytes=budget_bytes,
        symbols=["AAPL"],
        days=2,
        max_workers=max_workers,
    )


def test_fetch_tier_writes_partitions_and_manifest(tmp_path) -> None:
    config = _config(tmp_path, budget_bytes=10**12)
    client = MockDataClient()
    written, _bytes = raw_backfill.fetch_tier(config, client, "trades", ["AAPL"], [DAY])
    assert written == 1
    out = tmp_path / "raw" / "trades" / "symbol=AAPL" / f"date={DAY.isoformat()}" / "data.parquet"
    assert out.exists()
    manifest = raw_backfill.load_manifest(str(tmp_path), "trades")
    assert manifest.height == 1
    assert manifest["rows"][0] == 1


def test_resume_skips_done_symbol_day(tmp_path) -> None:
    config = _config(tmp_path, budget_bytes=10**12)
    client = MockDataClient()
    raw_backfill.fetch_tier(config, client, "trades", ["AAPL"], [DAY])
    # second pass: already in manifest -> nothing new fetched
    written, _bytes = raw_backfill.fetch_tier(config, client, "trades", ["AAPL"], [DAY])
    assert written == 0


def test_budget_stop_prevents_writes(tmp_path) -> None:
    config = _config(tmp_path, budget_bytes=0)  # zero budget -> immediate STOP
    client = MockDataClient()
    written, _bytes = raw_backfill.fetch_tier(config, client, "trades", ["AAPL"], [DAY])
    assert written == 0


class MultiSymbolBarsClient:
    """Returns a one-row bar frame for ANY requested symbol; used for concurrency tests."""

    def get_stock_bars(self, request):  # type: ignore[no-untyped-def]
        symbol = request.symbol_or_symbols
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet({symbol: [FakeBar(ts, 1.0, 2.0, 0.5, 1.5, 100, 1.4, 9)]})


def test_parallel_fetch_no_duplicate_manifest_rows(tmp_path) -> None:
    symbols = [f"SYM{i:02d}" for i in range(15)]
    days = [DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)]
    config = raw_backfill.BackfillConfig(
        store=str(tmp_path),
        months=6,
        top_trades=2,
        top_quotes=1,
        budget_bytes=10**12,
        symbols=symbols,
        days=len(days),
        max_workers=8,
    )
    written, _bytes = raw_backfill.fetch_tier(
        config, MultiSymbolBarsClient(), "bars", symbols, days
    )
    assert written == len(symbols) * len(days)
    manifest = raw_backfill.load_manifest(str(tmp_path), "bars")
    assert manifest.height == len(symbols) * len(days)
    dupes = manifest.group_by(["tier", "symbol", "date"]).len().filter(pl.col("len") > 1)
    assert dupes.height == 0
    # idempotent re-run under concurrency fetches nothing new
    rewritten, _ = raw_backfill.fetch_tier(
        config, MultiSymbolBarsClient(), "bars", symbols, days
    )
    assert rewritten == 0


def test_parallel_budget_stop_halts(tmp_path) -> None:
    symbols = [f"SYM{i:02d}" for i in range(15)]
    days = [DAY, DAY + dt.timedelta(days=1)]
    config = raw_backfill.BackfillConfig(
        store=str(tmp_path),
        months=6,
        top_trades=2,
        top_quotes=1,
        budget_bytes=0,  # zero budget -> immediate STOP, even with many workers
        symbols=symbols,
        days=len(days),
        max_workers=8,
    )
    written, _bytes = raw_backfill.fetch_tier(
        config, MultiSymbolBarsClient(), "bars", symbols, days
    )
    assert written == 0


def test_rank_by_dollar_volume_reads_bars(tmp_path) -> None:
    store = str(tmp_path)
    for symbol, dollar in (("HIGH", 1000.0), ("LOW", 1.0)):
        out_dir = raw_backfill.partition_dir(store, "bars", symbol, DAY)
        os.makedirs(out_dir, exist_ok=True)
        pl.DataFrame({"close": [dollar], "volume": [1]}).write_parquet(
            os.path.join(out_dir, "data.parquet")
        )
    ranked = raw_backfill.rank_by_dollar_volume(store, ["LOW", "HIGH"], [DAY])
    assert ranked == ["HIGH", "LOW"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
