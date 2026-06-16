"""Unit tests for the shared /store/raw bars/trades/quotes backfill (fetchers + orchestrator).

No network: Alpaca clients are mocked at module level and injected via `_thread_client`. Covers fetcher
normalization/schema, the SIP-feed + raw-adjustment request shape, multi-symbol + date-range batching,
per-day partition splitting, manifest resume (skip-done), the budget/headroom STOP, and dollar-volume
ranking from on-disk bars.
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
    fetch_bars_multi,
    fetch_quotes_day,
    fetch_trades_day,
    fetch_trades_range,
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


def _as_list(symbol_or_symbols) -> list[str]:  # type: ignore[no-untyped-def]
    if isinstance(symbol_or_symbols, str):
        return [symbol_or_symbols]
    return list(symbol_or_symbols)


class MockDataClient:
    """One row per requested symbol at DAY 14:30Z; records the last request to assert SIP/RAW shape.

    Handles both a single-symbol string and a multi-symbol list (and a date range — the single canned
    bar's timestamp falls on DAY, so range requests place it in the DAY partition and leave other days
    empty)."""

    def __init__(self) -> None:
        self.last_request = None

    def get_stock_bars(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet(
            {symbol: [FakeBar(ts, 1.0, 2.0, 0.5, 1.5, 100, 1.4, 9)] for symbol in _as_list(request.symbol_or_symbols)}
        )

    def get_stock_trades(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet(
            {symbol: [FakeTrade(ts, 1.5, 100.0, "Q", ["@", "I"], "C", 42)] for symbol in _as_list(request.symbol_or_symbols)}
        )

    def get_stock_quotes(self, request):  # type: ignore[no-untyped-def]
        self.last_request = request
        ts = dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)
        return FakeSet(
            {symbol: [FakeQuote(ts, 1.4, 10.0, "P", 1.6, 12.0, "Q", ["R"], "C")] for symbol in _as_list(request.symbol_or_symbols)}
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


def test_fetch_bars_multi_returns_frame_per_symbol() -> None:
    client = MockDataClient()
    frames = fetch_bars_multi(client, ["AAPL", "MSFT", "NVDA"], DAY, DAY)
    assert set(frames.keys()) == {"AAPL", "MSFT", "NVDA"}
    assert all(frame.schema == BARS_SCHEMA and frame.height == 1 for frame in frames.values())
    # multi-symbol request carries a LIST and the SIP/RAW shape
    assert client.last_request.symbol_or_symbols == ["AAPL", "MSFT", "NVDA"]
    assert client.last_request.feed.value == "sip"
    assert client.last_request.adjustment.value == "raw"


def test_fetch_trades_range_is_single_symbol() -> None:
    client = MockDataClient()
    frame = fetch_trades_range(client, "AAPL", DAY, DAY)
    assert frame.schema == TRADES_SCHEMA
    assert frame.height == 1
    assert client.last_request.symbol_or_symbols == "AAPL"


def test_split_by_day_partitions_multi_day_frame() -> None:
    rows = {
        "ts": [
            dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 12, 15, 30, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 13, 14, 30, tzinfo=dt.timezone.utc),
        ],
        "v": [1, 2, 3],
    }
    frame = pl.DataFrame(rows)
    by_day = raw_backfill.split_by_day(frame)
    assert set(by_day.keys()) == {dt.date(2026, 6, 12), dt.date(2026, 6, 13)}
    assert by_day[dt.date(2026, 6, 12)].height == 2
    assert by_day[dt.date(2026, 6, 13)].height == 1


def _config(tmp_path, budget_bytes: int, max_workers: int = 1, symbols=None) -> raw_backfill.BackfillConfig:
    return raw_backfill.BackfillConfig(
        store=str(tmp_path),
        months=6,
        top_trades=2,
        top_quotes=1,
        budget_bytes=budget_bytes,
        symbols=symbols or ["AAPL"],
        days=2,
        max_workers=max_workers,
        bars_symbols_per_request=100,
        bars_chunk_days=30,
        trades_chunk_days=5,
        quotes_chunk_days=2,
        processes=4,
        threads_per_process=4,
    )


def test_fetch_ticks_tier_writes_partitions_and_manifest(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    assert written == 1
    out = tmp_path / "raw" / "trades" / "symbol=AAPL" / f"date={DAY.isoformat()}" / "data.parquet"
    assert out.exists()
    manifest = raw_backfill.load_manifest(str(tmp_path), "trades")
    assert manifest.height == 1
    assert manifest["rows"][0] == 1


def test_resume_skips_done_symbol_day(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    # second pass: already in manifest -> nothing new fetched
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    assert written == 0


def test_budget_stop_prevents_writes(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, budget_bytes=0)  # zero budget -> immediate STOP
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    assert written == 0


def test_bars_tier_multi_symbol_writes_every_pending_day(tmp_path, monkeypatch) -> None:
    symbols = [f"SYM{i:02d}" for i in range(15)]
    days = [DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)]
    config = _config(tmp_path, budget_bytes=10**12, max_workers=8, symbols=symbols)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    written, _bytes = raw_backfill.fetch_bars_tier(config, symbols, days)
    # every (symbol, day) partition is written — days the mock returns no bar for get an empty partition
    assert written == len(symbols) * len(days)
    manifest = raw_backfill.load_manifest(str(tmp_path), "bars")
    assert manifest.height == len(symbols) * len(days)
    dupes = manifest.group_by(["tier", "symbol", "date"]).len().filter(pl.col("len") > 1)
    assert dupes.height == 0
    # the DAY partition has the canned bar; later days are empty but DONE
    day0 = pl.read_parquet(
        tmp_path / "raw" / "bars" / "symbol=SYM00" / f"date={DAY.isoformat()}" / "data.parquet"
    )
    assert day0.height == 1
    # idempotent re-run fetches nothing new
    rewritten, _ = raw_backfill.fetch_bars_tier(config, symbols, days)
    assert rewritten == 0


def test_bars_tier_budget_stop_halts(tmp_path, monkeypatch) -> None:
    symbols = [f"SYM{i:02d}" for i in range(15)]
    days = [DAY, DAY + dt.timedelta(days=1)]
    config = _config(tmp_path, budget_bytes=0, max_workers=8, symbols=symbols)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    written, _bytes = raw_backfill.fetch_bars_tier(config, symbols, days)
    assert written == 0


def test_manifest_unions_legacy_file_and_parts(tmp_path) -> None:
    """Resume correctness: load_manifest must union a legacy single-file manifest (what the live run
    already wrote) with the new append-only part files."""
    store = str(tmp_path)
    os.makedirs(os.path.join(store, "raw"), exist_ok=True)
    now = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    legacy = pl.DataFrame(
        [{"tier": "bars", "symbol": "OLD", "date": "2026-06-10", "rows": 5, "bytes": 9, "fetched_at": now}],
        schema=raw_backfill.MANIFEST_SCHEMA,
    )
    legacy.write_parquet(raw_backfill.manifest_path(store, "bars"))
    raw_backfill.write_manifest_part(
        store, "bars",
        [{"tier": "bars", "symbol": "NEW", "date": "2026-06-11", "rows": 7, "bytes": 8, "fetched_at": now}],
        part_seq=1,
    )
    merged = raw_backfill.load_manifest(store, "bars")
    assert merged.height == 2
    assert raw_backfill.done_keys(merged) == {("OLD", "2026-06-10"), ("NEW", "2026-06-11")}


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


def test_rank_sampling_uses_recent_days(tmp_path) -> None:
    """Recent-day sampling ranks by recent liquidity; sample_days=0 scores the full history."""
    store = str(tmp_path)
    days = [DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)]

    def _write(symbol: str, day: dt.date, close: float, volume: int) -> None:
        out_dir = raw_backfill.partition_dir(store, "bars", symbol, day)
        os.makedirs(out_dir, exist_ok=True)
        pl.DataFrame({"close": [close], "volume": [volume]}).write_parquet(
            os.path.join(out_dir, "data.parquet")
        )

    _write("OLD_HEAVY", days[0], 100.0, 1_000_000)  # huge, only on the OLDEST day
    _write("RECENT", days[2], 10.0, 1000)  # small, only on the MOST RECENT day

    # sample only the most recent day -> RECENT wins (OLD_HEAVY scores 0 in-window)
    ranked_recent = raw_backfill.rank_by_dollar_volume(store, ["OLD_HEAVY", "RECENT"], days, sample_days=1)
    assert ranked_recent == ["RECENT", "OLD_HEAVY"]
    # score the full history -> OLD_HEAVY's old volume dominates
    ranked_all = raw_backfill.rank_by_dollar_volume(store, ["OLD_HEAVY", "RECENT"], days, sample_days=0)
    assert ranked_all == ["OLD_HEAVY", "RECENT"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
