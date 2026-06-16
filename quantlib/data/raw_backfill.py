"""Resumable, space-aware orchestrator for the shared `/store/raw/` 6-month raw dataset.

For the last ~N trading days it:
  1. Fetches minute BARS for ALL universe symbols (cheap) and ranks symbols by dollar-volume.
  2. Fetches raw TRADES for the top-liquid `top_trades` symbols.
  3. Fetches raw QUOTES for the top-liquid `top_quotes` symbols (`top_quotes` < `top_trades` — quotes
     are ~10-50x trade volume).

Priority is bars > trades > quotes, liquid-first; a tier STOPS once the on-disk budget headroom is
exhausted. Each fetched (tier, symbol, date) is recorded in a per-tier MANIFEST parquet, so a re-run
SKIPS what is already on disk and RESUMES an interrupted run — idempotent.

Layout:  /store/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet
Manifest: /store/raw/_manifest_<tier>.parquet  (tier, symbol, date, rows, bytes, fetched_at)

Run inside the fp-dev image with the /store volume mounted and Alpaca creds in env:
    python -m quantlib.data.raw_backfill --months 6 --top-trades 1500 --top-quotes 300 \
        --budget-tb 1.8 --store /store
A `--symbols AAPL,SPY,NVDA --days 2` sample mode fetches a tiny set for evidence without ranking.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import polars as pl
from alpaca.data.historical import StockHistoricalDataClient
from requests.adapters import HTTPAdapter
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest, GetCalendarRequest

from quantlib.data.raw_fetchers import (
    fetch_bars_day,
    fetch_quotes_day,
    fetch_trades_day,
)
from quantlib.universe import is_etf_like

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("raw_backfill")

TIERS = ("bars", "trades", "quotes")
DEFAULT_STORE = "/store"
SAFETY_HEADROOM_BYTES = 50 * 1024**3  # never fill the last 50 GB of the budget
BYTES_PER_TB = 1024**4
DEFAULT_MAX_WORKERS = 12
HTTP_POOL_SIZE = 64  # HTTP connection pool >> worker count so parallel fetches reuse, not churn, connections

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "tier": pl.String,
    "symbol": pl.String,
    "date": pl.String,
    "rows": pl.Int64,
    "bytes": pl.Int64,
    "fetched_at": pl.Datetime("us", "UTC"),
}

_FETCHERS = {
    "bars": fetch_bars_day,
    "trades": fetch_trades_day,
    "quotes": fetch_quotes_day,
}


@dataclass
class BackfillConfig:
    store: str
    months: int
    top_trades: int
    top_quotes: int
    budget_bytes: int
    symbols: list[str] | None  # explicit sample set; None => full universe
    days: int | None  # explicit day count for sample mode; None => `months` of trading days
    max_workers: int  # thread-pool size for concurrent per-symbol-day fetching


def trading_client() -> TradingClient:
    return TradingClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )


def data_client() -> StockHistoricalDataClient:
    client = StockHistoricalDataClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
    )
    # Size the HTTP connection pool to the concurrent worker count so parallel fetches REUSE connections
    # instead of churning ("connection pool is full, discarding connection") — that churn serializes/stalls
    # the threads (observed: 0 progress at 10 workers on the default pool of 10). alpaca-py keeps a
    # requests.Session at ``_session``; mount a bigger pool on it (defensive getattr if the attr moves).
    session = getattr(client, "_session", None)
    if session is not None:
        adapter = HTTPAdapter(pool_connections=HTTP_POOL_SIZE, pool_maxsize=HTTP_POOL_SIZE)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return client


def trading_days(client: TradingClient, start: dt.date, end: dt.date) -> list[dt.date]:
    """Real NYSE trading days in [start, end] via the Alpaca calendar."""
    calendar = client.get_calendar(GetCalendarRequest(start=start, end=end))
    return sorted({entry.date for entry in calendar})


def universe_symbols(client: TradingClient) -> list[str]:
    """All active, tradable US-equity single names (ETF-like products screened out by name)."""
    assets = client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    symbols = [
        asset.symbol
        for asset in assets
        if asset.tradable and "/" not in asset.symbol and not is_etf_like(asset.name)
    ]
    return sorted(set(symbols))


def partition_dir(store: str, tier: str, symbol: str, day: dt.date) -> str:
    return os.path.join(store, "raw", tier, f"symbol={symbol}", f"date={day.isoformat()}")


def manifest_path(store: str, tier: str) -> str:
    return os.path.join(store, "raw", f"_manifest_{tier}.parquet")


def load_manifest(store: str, tier: str) -> pl.DataFrame:
    path = manifest_path(store, tier)
    if os.path.exists(path):
        return pl.read_parquet(path)
    return pl.DataFrame(schema=MANIFEST_SCHEMA)


def done_keys(manifest: pl.DataFrame) -> set[tuple[str, str]]:
    """Set of (symbol, date) already recorded in a tier manifest."""
    if manifest.height == 0:
        return set()
    return {
        (symbol, date)
        for symbol, date in zip(
            manifest["symbol"].to_list(), manifest["date"].to_list()
        )
    }


def append_manifest(store: str, tier: str, manifest: pl.DataFrame, entry: dict) -> pl.DataFrame:
    """Append one entry and persist the tier manifest atomically (write-tmp-then-rename)."""
    row = pl.DataFrame([entry], schema=MANIFEST_SCHEMA)
    updated = pl.concat([manifest, row], how="vertical") if manifest.height else row
    path = manifest_path(store, tier)
    # Per-process unique tmp name: within a process the lock serializes this, and a per-pid tmp keeps a
    # transient cross-process overlap (e.g. an old run still winding down while a new one starts) from
    # racing on a shared tmp -> rename (each process renames only its OWN tmp; no FileNotFoundError).
    tmp_path = f"{path}.{os.getpid()}.tmp"
    updated.write_parquet(tmp_path)
    os.replace(tmp_path, path)
    return updated


def free_bytes(store: str) -> int:
    stats = os.statvfs(store)
    return stats.f_bavail * stats.f_frsize


def write_partition(store: str, tier: str, symbol: str, day: dt.date, frame: pl.DataFrame) -> int:
    """Write a symbol-day partition parquet; return on-disk byte size. Empty frames still write a
    zero-row file so the manifest marks the symbol-day DONE (no-data days are not re-fetched)."""
    out_dir = partition_dir(store, tier, symbol, day)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.parquet")
    tmp_path = os.path.join(out_dir, "data.parquet.tmp")
    frame.write_parquet(tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)
    return os.path.getsize(out_path)


class _TierProgress:
    """Thread-safe shared state for a tier's concurrent fetch.

    All mutation of the manifest, the done-key set, the running byte total and the stop flag happens
    under a SINGLE lock so that workers fetching different symbols cannot: (a) double-fetch the same
    (symbol, date), (b) corrupt the manifest parquet on append, or (c) overshoot the disk budget. The
    fetch + parquet write themselves run OUTSIDE the lock (the slow, IO-bound part) so threads make
    real concurrent progress; only the small book-keeping critical sections serialize.
    """

    def __init__(self, store: str, tier: str, budget_bytes: int) -> None:
        self.store = store
        self.tier = tier
        self.budget_bytes = budget_bytes
        self.lock = threading.Lock()
        self.manifest = load_manifest(store, tier)
        self.done = done_keys(self.manifest)
        self.written = 0
        self.bytes_written = 0
        self.budget_used = int(self.manifest["bytes"].sum()) if self.manifest.height else 0
        self.stopped = False

    def claim(self, key: tuple[str, str]) -> bool:
        """Atomically reserve a (symbol, date) for fetching. Returns False if already done, if the
        budget/headroom STOP has tripped, or if another worker just claimed it — the caller then
        skips. Marking the key here (before the slow fetch) prevents two threads claiming the same
        pair; a fetch failure would leave it claimed, which is acceptable (a re-run re-fetches only
        pairs missing from the persisted manifest, not the in-memory claim set)."""
        with self.lock:
            if self.stopped or key in self.done:
                return False
            disk_free = free_bytes(self.store)
            if disk_free <= SAFETY_HEADROOM_BYTES or self.budget_used >= self.budget_bytes:
                if not self.stopped:
                    self.stopped = True
                    logger.warning(
                        "tier=%s STOP: budget/headroom reached (free=%.1fGB, used=%.2fTB/%.2fTB)",
                        self.tier,
                        disk_free / 1024**3,
                        self.budget_used / BYTES_PER_TB,
                        self.budget_bytes / BYTES_PER_TB,
                    )
                return False
            self.done.add(key)
            return True

    def record(self, entry: dict, size: int) -> None:
        """Atomically persist a fetched partition's manifest entry and update the byte totals."""
        with self.lock:
            self.manifest = append_manifest(self.store, self.tier, self.manifest, entry)
            self.written += 1
            self.bytes_written += size
            self.budget_used += size


def _fetch_one(
    progress: _TierProgress,
    client: StockHistoricalDataClient,
    tier: str,
    symbol: str,
    day: dt.date,
) -> None:
    """Worker: claim, fetch and persist a single (symbol, day) partition if not already done/stopped."""
    key = (symbol, day.isoformat())
    if not progress.claim(key):
        return
    fetcher = _FETCHERS[tier]
    frame = fetcher(client, symbol, day)
    size = write_partition(progress.store, tier, symbol, day, frame)
    entry = {
        "tier": tier,
        "symbol": symbol,
        "date": day.isoformat(),
        "rows": frame.height,
        "bytes": size,
        "fetched_at": dt.datetime.now(dt.timezone.utc),
    }
    progress.record(entry, size)


def fetch_tier(
    config: BackfillConfig,
    client: StockHistoricalDataClient,
    tier: str,
    symbols: list[str],
    days: list[dt.date],
) -> tuple[int, int]:
    """Fetch every (symbol, day) for a tier across a thread pool, liquid-first, skipping manifest-done
    pairs and stopping when free space drops below the budget headroom. Returns
    (partitions_written, bytes_written).

    Concurrency is bounded by `config.max_workers` (default 12); the per-call `_with_retry` back-off in
    the fetchers still handles 429/5xx so rate-limit pushback is respected even when many symbols are
    in flight. Work is submitted symbol-major (liquid-first) so the highest-value symbols are claimed
    first; once the disk-budget STOP trips, in-flight tasks finish and all remaining ones no-op via the
    `claim` guard, so the tier winds down cleanly."""
    progress = _TierProgress(config.store, tier, config.budget_bytes)
    pairs = [(symbol, day) for symbol in symbols for day in days]
    with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as executor:
        futures = [
            executor.submit(_fetch_one, progress, client, tier, symbol, day)
            for symbol, day in pairs
        ]
        for future in as_completed(futures):
            future.result()
    logger.info(
        "tier=%s done (%d partitions, %.3fGB this run, %d workers)",
        tier,
        progress.written,
        progress.bytes_written / 1024**3,
        config.max_workers,
    )
    return progress.written, progress.bytes_written


def rank_by_dollar_volume(store: str, symbols: list[str], days: list[dt.date]) -> list[str]:
    """Rank symbols by total dollar-volume from the already-fetched BARS partitions (close*volume).

    Bars are fetched first for the whole universe, so this reads the on-disk bars manifest's
    partitions rather than re-hitting the API. Symbols with no bars sort last."""
    scores: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    for symbol in symbols:
        for day in days:
            path = os.path.join(
                partition_dir(store, "bars", symbol, day), "data.parquet"
            )
            if not os.path.exists(path):
                continue
            frame = pl.read_parquet(path, columns=["close", "volume"])
            if frame.height:
                scores[symbol] += float(
                    (frame["close"] * frame["volume"]).sum()
                )
    return sorted(symbols, key=lambda symbol: scores[symbol], reverse=True)


def run(config: BackfillConfig) -> None:
    os.makedirs(os.path.join(config.store, "raw"), exist_ok=True)
    trade_client = trading_client()
    hist_client = data_client()

    today = dt.datetime.now(dt.timezone.utc).date()
    if config.symbols is not None and config.days is not None:
        all_days = trading_days(trade_client, today - dt.timedelta(days=14), today)
        days = all_days[-config.days :]
        universe = config.symbols
        logger.info("SAMPLE mode: %d symbols x %d days", len(universe), len(days))
    else:
        lookback = int(config.months * 31) + 7
        days = trading_days(trade_client, today - dt.timedelta(days=lookback), today)
        days = days[-int(config.months * 21) :]
        universe = universe_symbols(trade_client)
        logger.info("FULL mode: %d universe symbols x %d trading days", len(universe), len(days))

    logger.info(
        "disk free=%.1fGB, budget=%.2fTB", free_bytes(config.store) / 1024**3,
        config.budget_bytes / BYTES_PER_TB,
    )

    bars_written, bars_bytes = fetch_tier(config, hist_client, "bars", universe, days)
    logger.info("BARS: %d partitions, %.3fGB", bars_written, bars_bytes / 1024**3)

    ranked = rank_by_dollar_volume(config.store, universe, days)
    trade_symbols = ranked[: config.top_trades]
    quote_symbols = ranked[: config.top_quotes]
    logger.info(
        "ranked %d symbols; trades top-%d, quotes top-%d",
        len(ranked), len(trade_symbols), len(quote_symbols),
    )

    trades_written, trades_bytes = fetch_tier(config, hist_client, "trades", trade_symbols, days)
    logger.info("TRADES: %d partitions, %.3fGB", trades_written, trades_bytes / 1024**3)

    quotes_written, quotes_bytes = fetch_tier(config, hist_client, "quotes", quote_symbols, days)
    logger.info("QUOTES: %d partitions, %.3fGB", quotes_written, quotes_bytes / 1024**3)

    logger.info(
        "DONE: bars=%.3fGB trades=%.3fGB quotes=%.3fGB total=%.3fGB",
        bars_bytes / 1024**3, trades_bytes / 1024**3, quotes_bytes / 1024**3,
        (bars_bytes + trades_bytes + quotes_bytes) / 1024**3,
    )


def parse_args(argv: list[str]) -> BackfillConfig:
    parser = argparse.ArgumentParser(description="Resumable raw bars/trades/quotes backfill")
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--top-trades", type=int, default=1500)
    parser.add_argument("--top-quotes", type=int, default=300)
    parser.add_argument("--budget-tb", type=float, default=1.8)
    parser.add_argument("--symbols", default=None, help="comma list => SAMPLE mode")
    parser.add_argument("--days", type=int, default=None, help="recent trading days for SAMPLE mode")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("RAW_BACKFILL_WORKERS", DEFAULT_MAX_WORKERS)),
        help="thread-pool size for concurrent per-symbol-day fetching (env RAW_BACKFILL_WORKERS)",
    )
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    return BackfillConfig(
        store=args.store,
        months=args.months,
        top_trades=args.top_trades,
        top_quotes=args.top_quotes,
        budget_bytes=int(args.budget_tb * BYTES_PER_TB),
        symbols=symbols,
        days=args.days,
        max_workers=args.max_workers,
    )


def main() -> None:
    run(parse_args(sys.argv[1:]))


if __name__ == "__main__":
    main()
