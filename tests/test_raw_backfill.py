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


def _pin_resume_today(monkeypatch, today: dt.date) -> None:
    """Pin the rows-aware resume's reference date so the settle window is deterministic (not wall-clock)."""
    monkeypatch.setattr(raw_backfill, "_utc_today", lambda: today)


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
        start=None,
        end=None,
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
    # Pin the resume's "today" far AFTER these days so the empty no-data partitions are past the settle
    # window (aged-out genuine no-data, not a premature unsettled fetch) and the idempotency contract holds.
    _pin_resume_today(monkeypatch, DAY + dt.timedelta(days=400))
    written, _bytes = raw_backfill.fetch_bars_tier(config, symbols, days)
    # every (symbol, day) partition is written — days the mock returns no bar for get an empty partition
    assert written == len(symbols) * len(days)
    manifest = raw_backfill.load_manifest(str(tmp_path), "bars")
    assert manifest.height == len(symbols) * len(days)
    dupes = manifest.group_by(["tier", "symbol", "date"]).len().filter(pl.col("len") > 1)
    assert dupes.height == 0
    # the DAY partition has the canned bar; later days are empty but DONE (aged out of the settle window)
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


def _manifest_row(symbol: str, date: str, rows: int, fetched: dt.datetime) -> dict:
    return {"tier": "trades", "symbol": symbol, "date": date, "rows": rows, "bytes": 7, "fetched_at": fetched}


def test_resumable_done_keys_rows_aware_settle_window() -> None:
    """The rows-aware resume set: real rows are always done; a RECENT empty is NOT done (re-fetch the
    premature/unsettled entry); an OLD empty IS done (genuine no-data, never churned); and the MAX rows
    per key wins so a later real fetch supersedes an earlier poisoned 0-row entry for the same key."""
    today = dt.date(2026, 6, 19)
    now = dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
    manifest = pl.DataFrame(
        [
            _manifest_row("REAL", "2026-06-18", 100, now),  # recent + real -> done
            _manifest_row("EMPTY_RECENT", "2026-06-18", 0, now),  # recent + empty -> NOT done (re-fetch)
            _manifest_row("EMPTY_OLD", "2026-01-02", 0, now),  # old + empty -> done (genuine no-data)
            _manifest_row("SUPERSEDED", "2026-06-18", 0, now),  # poisoned 0-row...
            _manifest_row("SUPERSEDED", "2026-06-18", 500, now),  # ...later real fetch for same key -> done
        ],
        schema=raw_backfill.MANIFEST_SCHEMA,
    )
    resumable = raw_backfill.resumable_done_keys(manifest, today, settle_window_days=5)
    assert ("REAL", "2026-06-18") in resumable
    assert ("EMPTY_OLD", "2026-01-02") in resumable
    assert ("SUPERSEDED", "2026-06-18") in resumable  # max rows (500) > 0
    assert ("EMPTY_RECENT", "2026-06-18") not in resumable  # the poison case: re-fetched


def test_resumable_done_keys_pinned_ticker_stub_floor() -> None:
    """The pinned-ticker floor: a TINY non-zero tape (SPY trades=2, a pre-settle stub) is NOT done so it is
    re-fetched, while the SAME 2-row tape for a NON-pinned illiquid name IS done (genuine 2-trade day, never
    churned), and rows==0 is re-fetched regardless. This separates the impossible-real liquid stub that blocks
    the sweep from a genuinely-thin microcap, without a flat row floor that would churn illiquid names."""
    today = dt.date(2026, 6, 19)
    now = dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
    manifest = pl.DataFrame(
        [
            _manifest_row("SPY", "2026-06-18", 2, now),  # pinned + tiny stub -> NOT done (re-fetch)
            _manifest_row("ILLIQ", "2026-06-18", 2, now),  # non-pinned + genuine 2 trades -> done
            _manifest_row("SPY", "2026-06-17", 50000, now),  # pinned + real tape -> done
            _manifest_row("SPY", "2026-01-02", 2, now),  # pinned + OLD stub -> done (aged out, don't churn)
        ],
        schema=raw_backfill.MANIFEST_SCHEMA,
    )
    resumable = raw_backfill.resumable_done_keys(
        manifest,
        today,
        settle_window_days=5,
        force_refetch_symbols=frozenset({"SPY", "QQQ"}),
        min_settled_rows=100,
    )
    assert ("SPY", "2026-06-18") not in resumable  # the SPY=2 sweep blocker: re-fetched
    assert ("ILLIQ", "2026-06-18") in resumable  # illiquid 2-trade day: NOT churned
    assert ("SPY", "2026-06-17") in resumable  # pinned real tape: done
    assert ("SPY", "2026-01-02") in resumable  # pinned old stub: aged out


def test_fetch_ticks_tier_refetches_recent_empty_entry(tmp_path, monkeypatch) -> None:
    """End-to-end: a RECENT empty (0-row) trades manifest entry — the exact 06-18 poison (a fetch that beat
    Alpaca's symbol-by-symbol settle) — is RE-FETCHED on the next run and overwrites the empty partition with
    the now-settled tape, instead of being permanently stranded by presence-only resume."""
    store = str(tmp_path)
    os.makedirs(os.path.join(store, "raw"), exist_ok=True)
    # Seed the poison: an empty partition + a 0-row "done" manifest entry for (AAPL, DAY), recorded recently.
    fetched = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    raw_backfill.write_partition(store, "trades", "AAPL", DAY, pl.DataFrame(schema=TRADES_SCHEMA))
    raw_backfill.write_manifest_part(
        store, "trades", [_manifest_row("AAPL", DAY.isoformat(), 0, fetched)], 1
    )
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    _pin_resume_today(monkeypatch, DAY + dt.timedelta(days=2))  # DAY is within the 5-day settle window
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    assert written == 1  # the recent empty was re-fetched, not skipped
    part = pl.read_parquet(
        tmp_path / "raw" / "trades" / "symbol=AAPL" / f"date={DAY.isoformat()}" / "data.parquet"
    )
    assert part.height == 1  # the empty partition is overwritten with the settled tape


def test_fetch_ticks_tier_skips_recent_real_entry(tmp_path, monkeypatch) -> None:
    """Control for the re-fetch: a RECENT entry with real rows is still skipped (idempotent) — only EMPTY
    recent entries are reconsidered, so a normal nightly run does not re-pull settled tapes."""
    store = str(tmp_path)
    os.makedirs(os.path.join(store, "raw"), exist_ok=True)
    real_trade = pl.DataFrame(
        {
            "symbol": ["AAPL"],
            "ts": [dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.timezone.utc)],
            "price": [1.5],
            "size": [100.0],
            "exchange": ["Q"],
            "conditions": ["@"],
            "tape": ["C"],
            "trade_id": [1],
        },
        schema=TRADES_SCHEMA,
    )
    fetched = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    raw_backfill.write_partition(store, "trades", "AAPL", DAY, real_trade)
    raw_backfill.write_manifest_part(
        store, "trades", [_manifest_row("AAPL", DAY.isoformat(), 1, fetched)], 1
    )
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    _pin_resume_today(monkeypatch, DAY + dt.timedelta(days=2))  # recent, but rows>0 -> done
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [DAY], chunk_days=5)
    assert written == 0  # real recent entry is skipped


def test_fetch_ticks_tier_refetches_pinned_ticker_stub(tmp_path, monkeypatch) -> None:
    """End-to-end: a pinned market ticker with a tiny pre-settle stub (SPY trades=2 recorded "done") is
    RE-FETCHED — rows>0 alone would have stranded it and blocked the sweep's market reference. A non-pinned
    name with the same 2-row tape is NOT re-fetched (genuine illiquid day, no churn)."""
    store = str(tmp_path)
    os.makedirs(os.path.join(store, "raw"), exist_ok=True)
    pinned = sorted(raw_backfill.FORCE_REFETCH_SYMBOLS)[0]  # SPY/QQQ
    fetched = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    for symbol in (pinned, "ILLIQ"):
        raw_backfill.write_partition(store, "trades", symbol, DAY, pl.DataFrame(schema=TRADES_SCHEMA))
        raw_backfill.write_manifest_part(store, "trades", [_manifest_row(symbol, DAY.isoformat(), 2, fetched)], 1)
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    _pin_resume_today(monkeypatch, DAY + dt.timedelta(days=2))  # within the settle window
    written, _bytes = raw_backfill.fetch_ticks_tier(config, "trades", [pinned, "ILLIQ"], [DAY], chunk_days=5)
    assert written == 1  # only the pinned stub is re-fetched (the illiquid 2-row day is left done)


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


@dataclass
class FakeAsset:
    symbol: str
    name: str
    tradable: bool = True


class FakeTradingClient:
    """A trading client whose universe is a single common stock + screened ETFs, to prove the market
    tickers are force-included despite the ETF screen."""

    def __init__(self, assets: list[FakeAsset]) -> None:
        self._assets = assets

    def get_all_assets(self, _request: object) -> list[FakeAsset]:
        return self._assets


def test_universe_includes_market_tickers_despite_etf_screen() -> None:
    """SPY/QQQ are ETF-like (screened out as ordinary universe names) but MUST be in /store/raw for the
    cross-sectional features to validate — universe_symbols force-includes them."""
    assets = [
        FakeAsset("AAPL", "Apple Inc"),
        FakeAsset("SPY", "SPDR S&P 500 ETF Trust"),  # ETF-like -> would be screened
        FakeAsset("QQQ", "Invesco QQQ Trust"),  # ETF-like -> would be screened
    ]
    universe = raw_backfill.universe_symbols(FakeTradingClient(assets))
    for ticker in raw_backfill.MARKET_TICKERS:
        assert ticker in universe, f"{ticker} must be force-included for parity"
    assert "AAPL" in universe


def test_daily_mode_uses_full_universe_for_recent_days(tmp_path, monkeypatch) -> None:
    """A `--days N` run WITHOUT explicit symbols is DAILY mode: the full universe over the last N settled
    trading days (the self-sustaining nightly acquire), not the 6-month FULL window."""
    calendar_days = [DAY - dt.timedelta(days=2), DAY - dt.timedelta(days=1), DAY]
    monkeypatch.setattr(raw_backfill, "trading_client", lambda: object())
    monkeypatch.setattr(raw_backfill, "trading_days", lambda _client, _start, _end: calendar_days)
    monkeypatch.setattr(raw_backfill, "universe_symbols", lambda _client: ["AAPL", "SPY", "QQQ"])
    captured: dict[str, object] = {}

    def _capture_bars(_config: object, symbols: list[str], days: list[dt.date]) -> tuple[int, int]:
        captured["symbols"] = symbols
        captured["days"] = days
        return 0, 0

    monkeypatch.setattr(raw_backfill, "fetch_bars_tier", _capture_bars)
    monkeypatch.setattr(raw_backfill, "rank_by_dollar_volume", lambda *a, **k: ["AAPL"])
    monkeypatch.setattr(raw_backfill, "run_tier_fast", lambda *a, **k: (0, 0))

    config = raw_backfill.parse_args(["--store", str(tmp_path), "--days", "1"])
    assert config.symbols is None and config.days == 1  # DAILY mode (no symbols, days set)
    raw_backfill.run(config)
    assert captured["days"] == [DAY]  # only the last settled day
    assert set(captured["symbols"]) == {"AAPL", "SPY", "QQQ"}  # full universe


def test_parse_args_window_mode_sets_start_end() -> None:
    """--start/--end populate the WINDOW range as dates; --months keeps its default but is unused in
    WINDOW mode (the run() branch selects on start/end being set)."""
    config = raw_backfill.parse_args(["--start", "2025-09-01", "--end", "2026-03-01"])
    assert config.start == dt.date(2025, 9, 1)
    assert config.end == dt.date(2026, 3, 1)


def test_parse_args_no_window_leaves_start_end_none() -> None:
    """The additive default: with no window flags, start/end are None and every existing mode is reached
    exactly as before (the --months path is byte-unchanged)."""
    config = raw_backfill.parse_args(["--months", "6"])
    assert config.start is None and config.end is None
    assert config.months == 6


def test_parse_window_requires_both_flags() -> None:
    with pytest.raises(SystemExit):
        raw_backfill.parse_window("2025-09-01", None)
    with pytest.raises(SystemExit):
        raw_backfill.parse_window(None, "2026-03-01")


def test_parse_window_rejects_inverted_range() -> None:
    with pytest.raises(SystemExit):
        raw_backfill.parse_window("2026-03-01", "2025-09-01")


def test_window_mode_selects_explicit_range_over_full_universe(tmp_path, monkeypatch) -> None:
    """WINDOW mode fetches exactly the trading days in [start, end] over the full universe — it does NOT
    fall through to the months lookback, and the days passed to the tiers are the windowed calendar."""
    window_days = [dt.date(2025, 9, 2), dt.date(2025, 9, 3), dt.date(2025, 9, 4)]
    captured: dict[str, object] = {}

    def _capture_trading_days(_client, start, end):  # type: ignore[no-untyped-def]
        captured["start"] = start
        captured["end"] = end
        return window_days

    monkeypatch.setattr(raw_backfill, "trading_client", lambda: object())
    monkeypatch.setattr(raw_backfill, "trading_days", _capture_trading_days)
    monkeypatch.setattr(raw_backfill, "universe_symbols", lambda _client: ["AAPL", "SPY", "QQQ"])

    def _capture_bars(_config: object, symbols: list[str], days: list[dt.date]) -> tuple[int, int]:
        captured["symbols"] = symbols
        captured["days"] = days
        return 0, 0

    monkeypatch.setattr(raw_backfill, "fetch_bars_tier", _capture_bars)
    monkeypatch.setattr(raw_backfill, "rank_by_dollar_volume", lambda *a, **k: ["AAPL"])
    monkeypatch.setattr(raw_backfill, "run_tier_fast", lambda *a, **k: (0, 0))

    config = raw_backfill.parse_args(
        ["--store", str(tmp_path), "--start", "2025-09-01", "--end", "2025-09-05"]
    )
    raw_backfill.run(config)
    # the calendar query used the explicit window, NOT a months-lookback from today
    assert captured["start"] == dt.date(2025, 9, 1)
    assert captured["end"] == dt.date(2025, 9, 5)
    assert captured["days"] == window_days
    assert set(captured["symbols"]) == {"AAPL", "SPY", "QQQ"}


def test_window_mode_manifest_skips_present_cells(tmp_path, monkeypatch) -> None:
    """The core no-double-acquire guarantee in WINDOW mode: a (symbol, date) cell already present with
    rows>0 is skipped, so a windowed re-pull fetches nothing new. Drives the real fetch_ticks_tier path
    (the same manifest-skip every mode uses), proving WINDOW inherits idempotency by construction."""
    window_day = dt.date(2025, 9, 3)
    config = _config(tmp_path, budget_bytes=10**12)
    monkeypatch.setattr(raw_backfill, "_thread_client", lambda: MockDataClient())
    # pin resume "today" well after the window so the settled real rows are plainly done (not in-window)
    _pin_resume_today(monkeypatch, window_day + dt.timedelta(days=400))
    first, _ = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [window_day], chunk_days=5)
    assert first == 1  # cell fetched on the first windowed pass
    second, _ = raw_backfill.fetch_ticks_tier(config, "trades", ["AAPL"], [window_day], chunk_days=5)
    assert second == 0  # present cell skipped — no double-acquire on the overlapping window


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
