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

from quantlib.data.fast_backfill import (
    DEFAULT_PROCESSES,
    DEFAULT_THREADS_PER_PROCESS,
    run_tier_fast,
)
from quantlib.data.raw_fetchers import (
    fetch_bars_multi,
    fetch_quotes_range,
    fetch_trades_range,
)
from quantlib.data.raw_store import (
    MANIFEST_SCHEMA,
    done_keys,
    free_bytes,
    load_manifest,
    manifest_dir,
    manifest_path,
    partition_dir,
    reconcile_manifest_from_disk,
    write_manifest_part,
    write_partition,
)
from quantlib.features.groups.market_context import INDICES as MARKET_INDICES
from quantlib.universe import is_etf_like

# The cross-sectional market-reference tickers (SPY/QQQ). They are ETF-like, so the universe screen drops
# them — but the market-relative features (market_beta/market_corr/idio_vol/market_return/nasdaq_return/
# relative_return/outperforming) regress against them, so their backfill bars MUST be in /store/raw or
# those features can never validate (all-extra_live). We append them to the fetched universe unconditionally.
MARKET_TICKERS: tuple[str, ...] = tuple(sorted(set(MARKET_INDICES.values())))

__all__ = [
    "MANIFEST_SCHEMA",
    "done_keys",
    "free_bytes",
    "load_manifest",
    "manifest_dir",
    "manifest_path",
    "partition_dir",
    "reconcile_manifest_from_disk",
    "write_manifest_part",
    "write_partition",
]

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

# Request-batching knobs. A single Alpaca request can span a DATE RANGE (the SDK pages internally) and,
# for bars, MANY symbols — so one request replaces up to (symbols x days) per-symbol-day calls. We batch
# bars aggressively (small payloads) and chunk ticks conservatively (large payloads => bound peak memory,
# since the SDK accumulates all pages before returning). All are the SAME get_stock_* endpoints used by
# real-time validation, so a per-day slice of a batched response is cell-identical to fetching that day.
BARS_SYMBOLS_PER_REQUEST = 100  # symbols per multi-symbol bars request
BARS_CHUNK_DAYS = 30  # trading days per bars request
TRADES_CHUNK_DAYS = 5  # trading days per single-symbol trades request
# Quotes are ~10-50x trade volume and the SDK accumulates all pages before returning, so a multi-day
# quote chunk for a mega-cap can be tens of millions of rows held at once (a 6 GB sandbox OOM'd on a
# 2-day NVDA chunk). Default 1 day => same peak memory as the original per-day path; raise only when the
# run container has the RAM headroom (each concurrent worker holds one chunk).
QUOTES_CHUNK_DAYS = 1
MANIFEST_FLUSH_EVERY = (
    500  # buffer this many manifest entries before writing one append-only part file
)
RANK_SAMPLE_DAYS = (
    20  # rank dollar-volume on the most recent N days (liquidity tiering is day-stable)
)
RANK_WORKERS = (
    16  # concurrent symbol reads when ranking (IO-bound over many tiny parquet files)
)


@dataclass
class BackfillConfig:
    store: str
    months: int
    top_trades: int
    top_quotes: int
    budget_bytes: int
    symbols: list[str] | None  # explicit sample set; None => full universe
    days: (
        int | None
    )  # explicit day count for sample mode; None => `months` of trading days
    max_workers: int  # thread-pool size for concurrent request-units
    bars_symbols_per_request: int  # symbols per multi-symbol bars request
    bars_chunk_days: int  # trading days per bars request
    trades_chunk_days: int  # trading days per single-symbol trades request
    quotes_chunk_days: int  # trading days per single-symbol quotes request
    processes: int  # worker processes for the multiprocess tick engine
    threads_per_process: int  # threads per tick-engine worker process


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
        adapter = HTTPAdapter(
            pool_connections=HTTP_POOL_SIZE, pool_maxsize=HTTP_POOL_SIZE
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return client


_thread_local = threading.local()


def _thread_client() -> StockHistoricalDataClient:
    """One data client PER WORKER THREAD, so each thread has its OWN HTTP connection pool — this is what
    actually eliminates the cross-thread "pool is full, discarding connection" churn that serialized the
    parallel fetch (a single shared client = a single pool of 10, contended by all workers).
    """
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = data_client()
        _thread_local.client = client
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
    # The market-reference tickers are ETF-like (screened above) but their bars are REQUIRED in /store/raw
    # for the cross-sectional features to validate — always include them.
    return sorted(set(symbols) | set(MARKET_TICKERS))


class _TierProgress:
    """Thread-safe shared state for a tier's concurrent fetch.

    The done-key set, byte totals, stop flag and the manifest buffer mutate under a SINGLE lock so
    workers fetching different symbols cannot double-fetch a (symbol, date) or overshoot the budget.
    Manifest persistence is APPEND-ONLY + buffered: entries accumulate in memory and flush as one
    immutable part file every `MANIFEST_FLUSH_EVERY` (so recording a partition is O(buffer), not
    O(total manifest) — the batched fetch otherwise drowns in O(n^2) full-manifest rewrites). The
    fetch + parquet write run OUTSIDE the lock (the slow IO) so threads make real concurrent progress.
    """

    def __init__(self, store: str, tier: str, budget_bytes: int) -> None:
        self.store = store
        self.tier = tier
        self.budget_bytes = budget_bytes
        self.lock = threading.Lock()
        manifest = load_manifest(store, tier)
        self.done = done_keys(manifest)
        self.written = 0
        self.bytes_written = 0
        self.budget_used = int(manifest["bytes"].sum()) if manifest.height else 0
        self.stopped = False
        self._buffer: list[dict] = []
        self._part_seq = 0

    def should_stop(self) -> bool:
        """True once the disk-headroom / budget STOP has tripped (sticky). Checked at the start of every
        request-unit so a tier winds down cleanly: in-flight units finish, remaining ones no-op.
        """
        with self.lock:
            if self.stopped:
                return True
            disk_free = free_bytes(self.store)
            if (
                disk_free <= SAFETY_HEADROOM_BYTES
                or self.budget_used >= self.budget_bytes
            ):
                self.stopped = True
                logger.warning(
                    "tier=%s STOP: budget/headroom reached (free=%.1fGB, used=%.2fTB/%.2fTB)",
                    self.tier,
                    disk_free / 1024**3,
                    self.budget_used / BYTES_PER_TB,
                    self.budget_bytes / BYTES_PER_TB,
                )
                return True
            return False

    def pending_days(self, symbol: str, days: list[dt.date]) -> list[dt.date]:
        """Days in `days` not yet recorded done for `symbol` (manifest-resumable skip)."""
        with self.lock:
            return [day for day in days if (symbol, day.isoformat()) not in self.done]

    def record(self, entry: dict, size: int) -> None:
        """Mark a (symbol, date) done, buffer its manifest entry, update byte totals. Flushes the buffer
        to an append-only part file once it reaches MANIFEST_FLUSH_EVERY."""
        with self.lock:
            self.done.add((entry["symbol"], entry["date"]))
            self._buffer.append(entry)
            self.written += 1
            self.bytes_written += size
            self.budget_used += size
            if len(self._buffer) >= MANIFEST_FLUSH_EVERY:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        self._part_seq += 1
        write_manifest_part(self.store, self.tier, self._buffer, self._part_seq)
        self._buffer = []

    def flush(self) -> None:
        """Persist any buffered entries — call once a tier's thread pool drains so the tail isn't lost."""
        with self.lock:
            self._flush_locked()


def chunk_list(items: list, size: int) -> list[list]:
    """Split `items` into contiguous chunks of at most `size` (preserves order/liquidity ranking)."""
    return [items[i : i + size] for i in range(0, len(items), max(1, size))]


def split_by_day(frame: pl.DataFrame) -> dict[dt.date, pl.DataFrame]:
    """Partition a multi-day frame into {date: frame} by the UTC date of its `ts` column."""
    if frame.height == 0:
        return {}
    with_date = frame.with_columns(pl.col("ts").dt.date().alias("_day"))
    parts = with_date.partition_by("_day", as_dict=True)
    return {
        (key[0] if isinstance(key, tuple) else key): part.drop("_day")
        for key, part in parts.items()
    }


def _write_pending_days(
    progress: _TierProgress,
    tier: str,
    symbol: str,
    pending: list[dt.date],
    frame: pl.DataFrame,
) -> None:
    """Split one symbol's multi-day `frame` and write+record EVERY pending day — including days with no
    rows (an empty partition marks the day done so it is never re-fetched)."""
    by_day = split_by_day(frame)
    empty = frame.clear()
    for day in pending:
        day_frame = by_day.get(day, empty)
        size = write_partition(progress.store, tier, symbol, day, day_frame)
        progress.record(
            {
                "tier": tier,
                "symbol": symbol,
                "date": day.isoformat(),
                "rows": day_frame.height,
                "bytes": size,
                "fetched_at": dt.datetime.now(dt.timezone.utc),
            },
            size,
        )


def _fetch_bars_unit(
    progress: _TierProgress, symbols: list[str], day_chunk: list[dt.date]
) -> None:
    """One MULTI-SYMBOL bars request over a day-chunk: fetch all symbols' bars for [chunk0..chunkN] in a
    single paginated call, then split each symbol's response into its pending per-day partitions.
    """
    if progress.should_stop():
        return
    pending = {symbol: progress.pending_days(symbol, day_chunk) for symbol in symbols}
    active = [symbol for symbol, days in pending.items() if days]
    if not active:
        return
    frames = fetch_bars_multi(_thread_client(), active, day_chunk[0], day_chunk[-1])
    for symbol in active:
        _write_pending_days(progress, "bars", symbol, pending[symbol], frames[symbol])


def _fetch_ticks_unit(
    progress: _TierProgress, tier: str, symbol: str, day_chunk: list[dt.date], fetcher
) -> None:  # type: ignore[no-untyped-def]
    """One SINGLE-SYMBOL trades/quotes request over a day-chunk, split into pending per-day partitions."""
    if progress.should_stop():
        return
    pending = progress.pending_days(symbol, day_chunk)
    if not pending:
        return
    frame = fetcher(_thread_client(), symbol, day_chunk[0], day_chunk[-1])
    _write_pending_days(progress, tier, symbol, pending, frame)


def fetch_bars_tier(
    config: BackfillConfig, symbols: list[str], days: list[dt.date]
) -> tuple[int, int]:
    """Bars via MULTI-SYMBOL + DATE-RANGE requests: units = (symbol batch) x (day chunk). One request
    fetches up to `BARS_SYMBOLS_PER_REQUEST` symbols x `BARS_CHUNK_DAYS` days, collapsing the bars tier
    from ~(symbols x days) requests to ~(batches x chunks)."""
    progress = _TierProgress(config.store, "bars", config.budget_bytes)
    day_chunks = chunk_list(days, config.bars_chunk_days)
    symbol_batches = chunk_list(symbols, config.bars_symbols_per_request)
    units = [(batch, chunk) for batch in symbol_batches for chunk in day_chunks]
    with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as executor:
        futures = [
            executor.submit(_fetch_bars_unit, progress, batch, chunk)
            for batch, chunk in units
        ]
        for future in as_completed(futures):
            future.result()
    progress.flush()
    logger.info(
        "tier=bars done (%d partitions, %.3fGB this run, %d units, %d workers)",
        progress.written,
        progress.bytes_written / 1024**3,
        len(units),
        config.max_workers,
    )
    return progress.written, progress.bytes_written


def fetch_ticks_tier(
    config: BackfillConfig,
    tier: str,
    symbols: list[str],
    days: list[dt.date],
    chunk_days: int,
) -> tuple[int, int]:
    """Trades/quotes via SINGLE-SYMBOL + DATE-RANGE requests: units = (symbol) x (day chunk). One
    request fetches `chunk_days` days for a symbol (paginated), bounding peak memory on the large tick
    payloads while still cutting the per-day request count by ~`chunk_days`x."""
    fetcher = fetch_trades_range if tier == "trades" else fetch_quotes_range
    progress = _TierProgress(config.store, tier, config.budget_bytes)
    day_chunks = chunk_list(days, chunk_days)
    units = [(symbol, chunk) for symbol in symbols for chunk in day_chunks]
    with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as executor:
        futures = [
            executor.submit(_fetch_ticks_unit, progress, tier, symbol, chunk, fetcher)
            for symbol, chunk in units
        ]
        for future in as_completed(futures):
            future.result()
    progress.flush()
    logger.info(
        "tier=%s done (%d partitions, %.3fGB this run, %d units, %d workers)",
        tier,
        progress.written,
        progress.bytes_written / 1024**3,
        len(units),
        config.max_workers,
    )
    return progress.written, progress.bytes_written


def _symbol_dollar_volume(store: str, symbol: str, days: list[dt.date]) -> float:
    """Total close*volume for one symbol across `days`, reading its on-disk bars partitions."""
    total = 0.0
    for day in days:
        path = os.path.join(partition_dir(store, "bars", symbol, day), "data.parquet")
        if not os.path.exists(path):
            continue
        frame = pl.read_parquet(path, columns=["close", "volume"])
        if frame.height:
            total += float((frame["close"] * frame["volume"]).sum())
    return total


def rank_by_dollar_volume(
    store: str,
    symbols: list[str],
    days: list[dt.date],
    sample_days: int = RANK_SAMPLE_DAYS,
    max_workers: int = RANK_WORKERS,
) -> list[str]:
    """Rank symbols by dollar-volume (close*volume) from the already-fetched BARS partitions.

    Two ranking-stable speedups over the naive serial scan of every (symbol, day) file (the universe is
    ~7.6k symbols x 126 days ~ 960k tiny reads, slow single-threaded on a cold cache): (1) score only the
    most RECENT `sample_days` trading days — liquidity tiering (top-N for trades/quotes) is stable across
    days, so a recent window picks the same liquid names at a fraction of the I/O; (2) read symbols
    CONCURRENTLY across a thread pool (the work is IO-bound). Symbols with no bars score 0 and sort last.
    `sample_days <= 0` scores all days (the exact, slower form)."""
    scored_days = days[-sample_days:] if sample_days > 0 else days
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        totals = executor.map(
            lambda symbol: _symbol_dollar_volume(store, symbol, scored_days), symbols
        )
        scores = dict(zip(symbols, totals))
    return sorted(symbols, key=lambda symbol: scores[symbol], reverse=True)


def run(config: BackfillConfig) -> None:
    os.makedirs(os.path.join(config.store, "raw"), exist_ok=True)

    # Reconcile FIRST: an OOM/crash can lose a worker's unflushed manifest buffer even though its
    # partitions were already written to disk, so a naive resume re-fetches >100k complete tick units
    # (observed after two quotes-tier OOMs). Recording the orphaned on-disk partitions into the manifest
    # makes the resume skip them. Idempotent + cheap relative to fetching, so it runs every time.
    for tier in ("trades", "quotes"):
        reconcile_manifest_from_disk(config.store, tier)

    trade_client = trading_client()

    today = dt.datetime.now(dt.timezone.utc).date()
    if config.symbols is not None and config.days is not None:
        all_days = trading_days(trade_client, today - dt.timedelta(days=14), today)
        days = all_days[-config.days :]
        universe = config.symbols
        logger.info("SAMPLE mode: %d symbols x %d days", len(universe), len(days))
    elif config.days is not None:
        # DAILY mode: the full universe but only the last ``days`` settled trading days. This is the
        # self-sustaining nightly acquire — fetch the just-completed day's tape for the whole universe,
        # idempotent via the manifest (an already-fetched symbol-day is skipped on re-run).
        all_days = trading_days(trade_client, today - dt.timedelta(days=14), today)
        days = all_days[-config.days :]
        universe = universe_symbols(trade_client)
        logger.info(
            "DAILY mode: %d universe symbols x %d recent trading days",
            len(universe),
            len(days),
        )
    else:
        lookback = int(config.months * 31) + 7
        days = trading_days(trade_client, today - dt.timedelta(days=lookback), today)
        days = days[-int(config.months * 21) :]
        universe = universe_symbols(trade_client)
        logger.info(
            "FULL mode: %d universe symbols x %d trading days", len(universe), len(days)
        )

    logger.info(
        "disk free=%.1fGB, budget=%.2fTB",
        free_bytes(config.store) / 1024**3,
        config.budget_bytes / BYTES_PER_TB,
    )

    bars_written, bars_bytes = fetch_bars_tier(config, universe, days)
    logger.info("BARS: %d partitions, %.3fGB", bars_written, bars_bytes / 1024**3)

    ranked = rank_by_dollar_volume(config.store, universe, days)
    trade_symbols = ranked[: config.top_trades]
    quote_symbols = ranked[: config.top_quotes]
    logger.info(
        "ranked %d symbols; trades top-%d, quotes top-%d",
        len(ranked),
        len(trade_symbols),
        len(quote_symbols),
    )

    # Ticks ALWAYS use the direct-httpx multiprocess engine — it is strictly faster than the SDK path
    # (the GIL serializes alpaca-py's per-row parse), so there is no reason to ever choose "slow".
    logger.info(
        "tick engine: direct-httpx multiprocess (%d procs x %d threads)",
        config.processes,
        config.threads_per_process,
    )
    trades_written, trades_bytes = run_tier_fast(
        config.store,
        "trades",
        trade_symbols,
        days,
        config.processes,
        config.threads_per_process,
    )
    quotes_written, quotes_bytes = run_tier_fast(
        config.store,
        "quotes",
        quote_symbols,
        days,
        config.processes,
        config.threads_per_process,
    )
    logger.info("TRADES: %d partitions, %.3fGB", trades_written, trades_bytes / 1024**3)
    logger.info("QUOTES: %d partitions, %.3fGB", quotes_written, quotes_bytes / 1024**3)

    logger.info(
        "DONE: bars=%.3fGB trades=%.3fGB quotes=%.3fGB total=%.3fGB",
        bars_bytes / 1024**3,
        trades_bytes / 1024**3,
        quotes_bytes / 1024**3,
        (bars_bytes + trades_bytes + quotes_bytes) / 1024**3,
    )


def parse_args(argv: list[str]) -> BackfillConfig:
    parser = argparse.ArgumentParser(
        description="Resumable raw bars/trades/quotes backfill"
    )
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--top-trades", type=int, default=1500)
    parser.add_argument("--top-quotes", type=int, default=300)
    parser.add_argument("--budget-tb", type=float, default=1.8)
    parser.add_argument("--symbols", default=None, help="comma list => SAMPLE mode")
    parser.add_argument(
        "--days", type=int, default=None, help="recent trading days for SAMPLE mode"
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("RAW_BACKFILL_WORKERS", DEFAULT_MAX_WORKERS)),
        help="thread-pool size for concurrent request-units (env RAW_BACKFILL_WORKERS)",
    )
    parser.add_argument(
        "--bars-symbols-per-request", type=int, default=BARS_SYMBOLS_PER_REQUEST
    )
    parser.add_argument("--bars-chunk-days", type=int, default=BARS_CHUNK_DAYS)
    parser.add_argument("--trades-chunk-days", type=int, default=TRADES_CHUNK_DAYS)
    parser.add_argument("--quotes-chunk-days", type=int, default=QUOTES_CHUNK_DAYS)
    parser.add_argument(
        "--processes",
        type=int,
        default=int(os.environ.get("RAW_BACKFILL_PROCESSES", DEFAULT_PROCESSES)),
        help="worker processes for the multiprocess tick engine (env RAW_BACKFILL_PROCESSES)",
    )
    parser.add_argument(
        "--threads-per-process",
        type=int,
        default=int(
            os.environ.get("RAW_BACKFILL_THREADS", DEFAULT_THREADS_PER_PROCESS)
        ),
        help="threads per tick-engine worker process (env RAW_BACKFILL_THREADS)",
    )
    args = parser.parse_args(argv)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    )
    return BackfillConfig(
        store=args.store,
        months=args.months,
        top_trades=args.top_trades,
        top_quotes=args.top_quotes,
        budget_bytes=int(args.budget_tb * BYTES_PER_TB),
        symbols=symbols,
        days=args.days,
        max_workers=args.max_workers,
        bars_symbols_per_request=args.bars_symbols_per_request,
        bars_chunk_days=args.bars_chunk_days,
        trades_chunk_days=args.trades_chunk_days,
        quotes_chunk_days=args.quotes_chunk_days,
        processes=args.processes,
        threads_per_process=args.threads_per_process,
    )


def main() -> None:
    run(parse_args(sys.argv[1:]))


if __name__ == "__main__":
    main()
