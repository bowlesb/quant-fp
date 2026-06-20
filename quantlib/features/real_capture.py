"""Real-Alpaca capture adapter — connects via alpaca-py's StockDataStream and feeds the SHARED
``process_bars`` core (the same compute/store code as the mock; only the connection differs).

Alpaca delivers 1-minute bars one-at-a-time per symbol shortly after each minute closes; we batch by
minute and flush a completed minute to the core when the next minute's bars start arriving.
``STREAM_URL_OVERRIDE`` (env) can point at a protocol-faithful mock; unset = the real feed.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

import polars as pl
import redis

from quantlib.bus.market_data import MarketDataPublisher
from quantlib.features.backfill_bars import backfill_bars, backfill_daily
from quantlib.features.capture import (
    DEFAULT_BUFFER_MINUTES,
    CaptureState,
    process_bars,
    warm_start_enabled,
    warm_start_ring,
)
from quantlib.features import metrics
from quantlib.features.loaders import load_filings, load_reference, load_universe
from quantlib.features.sharded_capture import (
    INDEX_SYMBOLS,
    process_reduce,
    reduce_buffer_columns,
    reduce_buffer_minutes,
    route_minute,
    route_ticks,
    shard_of,
    worker_main,
)


# Tick AGGREGATION (the expensive part — per-symbol sign classification, spread/imbalance stats, the raw
# trades frame) now runs on the SHARD WORKER that owns each symbol, not inline on the single reader, so the
# firehose is distributed across the worker pool. The reader's residual per-tick cost is one hash + append
# to forward each raw tick to its shard's queue. The default is still a liquid canary set (a conservative
# rollout floor); ops scale toward the full universe with FP_TICK_SYMBOLS (a comma list, or "all"). The
# subscribed symbols light up trade_flow / quote_spread / liquidity (minute_agg tick columns) AND
# tick_runlength / microstructure_burst (the raw trades frame); the rest stay honest-null (we don't
# fabricate all-zero tick features for symbols we didn't subscribe).
DEFAULT_TICK_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "GOOG", "AMD",
    "NFLX", "JPM", "BAC", "XOM", "INTC", "F", "PLTR", "COIN", "AAPU", "SOXL", "TQQQ", "DIA",
)


def tick_symbols(universe: list[str]) -> list[str]:
    """Symbols to subscribe trades+quotes for (FP_TICK_SYMBOLS overrides: a comma list, or 'all')."""
    env = os.environ.get("FP_TICK_SYMBOLS", "").strip()
    if env == "all":
        return universe
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    universe_set = set(universe)
    return [s for s in DEFAULT_TICK_SYMBOLS if s in universe_set]


logger = logging.getLogger("real_capture")


def md_publish_enabled() -> bool:
    """``FP_PUBLISH_MD=1`` turns on the per-minute raw market-data streams (md:bar/trades/quotes).

    OFF by default — the live capture path is byte-for-byte unchanged until set. Checked ONCE at
    startup so the per-message hot path pays only a cheap boolean, never string parsing.
    """
    return os.environ.get("FP_PUBLISH_MD", "0") == "1"


def tick_publish_enabled() -> bool:
    """``FP_PUBLISH_TICKS=1`` turns on the tick-firehose streams (md:tick_trades/md:tick_quotes).

    OFF by default and additionally bounded by which symbols are actually tick-subscribed; see
    ``md_tick_symbols``. This is the heavy tier (~2-4k frames/s live) — only enable deliberately.
    """
    return os.environ.get("FP_PUBLISH_TICKS", "0") == "1"


def md_tick_symbols(subscribed: set[str]) -> set[str]:
    """Symbols to firehose individual ticks for. ``FP_TICK_SYMBOLS`` (the same allowlist that gates
    which symbols are tick-SUBSCRIBED) doubles as the firehose allowlist; empty = all subscribed.

    A firehose symbol must be tick-subscribed (we can't publish ticks we never receive), so the result
    is always the intersection with ``subscribed``.
    """
    env = os.environ.get("FP_TICK_SYMBOLS", "").strip()
    if env and env != "all":
        wanted = {s.strip().upper() for s in env.split(",") if s.strip()}
        return wanted & subscribed
    return set(subscribed)


def _group_ticks_by_symbol(ticks: list[dict]) -> dict[str, list[dict]]:
    """Bucket a minute's flat raw-tick dicts by symbol for the per-minute md:trades/md:quotes streams."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for tick in ticks:
        grouped[str(tick["S"])].append(tick)
    return grouped


def _shard_snapshots(snapshots: dict | None, symbols: list[str], shard_id: int, n_shards: int) -> dict | None:
    """Slice the reference/daily snapshots to just the symbols this shard owns (+ the replicated index
    ETFs) — so a spawned worker is handed only its share, not the whole universe's daily history."""
    if snapshots is None:
        return None
    shard_symbols = [s for s in symbols if shard_of(s, n_shards) == shard_id] + list(INDEX_SYMBOLS)
    keep = set(shard_symbols)
    return {name: frame.filter(pl.col("symbol").is_in(keep)) for name, frame in snapshots.items()}


def _reader_bench_path(root: str) -> Path | None:
    """Reader-side latency log (route+reduce) when FP_BENCH_LOG is set — benchmark/demo only."""
    if not os.environ.get("FP_BENCH_LOG"):
        return None
    path = Path(root) / "_bench" / "reader.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build_stream() -> StockDataStream:
    feed = DataFeed.SIP if os.environ.get("ALPACA_DATA_FEED", "sip").lower() == "sip" else DataFeed.IEX
    return StockDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=feed,
        url_override=os.environ.get("STREAM_URL_OVERRIDE"),
    )


def run_capture(symbols: list[str], root: str, mode: str, window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None) -> None:
    state = CaptureState()
    pending: dict = {"minute": None, "bars": []}
    # Load the slowly-changing snapshots ONCE at startup; held for the session so the sector/asset-flag
    # groups (reference) and the multi-day/prior-day groups (daily history) serve live off the SAME
    # frames the backfill + parity paths use. Daily needs the session date to anchor the prior close.
    snapshots = {"reference": load_reference()}
    if day is not None:
        snapshots["daily"] = backfill_daily(day, symbols)
        # Pin cross_sectional_rank to the day's FIXED in-universe set (gap #3) so ranks are over the same
        # membership live and backfill, not "whoever printed this minute" (a parity hazard).
        snapshots["universe"] = load_universe(day)
        # EDGAR session snapshot (filings event store with a 370d lookback); the per-minute
        # available_at<=minute gate inside the edgar group makes it point-in-time → backfill==live.
        snapshots["filings"] = load_filings(day)
    if warm_start_enabled() and day is not None:
        # Rehydrate the trailing ring from the session's settled bars (Alpaca historical RAW = the same
        # SIP tape the stream delivers) so a restart starts warm, not cold (CRITICAL-2; inert by default).
        seeded = warm_start_ring(state, backfill_bars(day, symbols), depth=window)
        print(f"[capture] warm-started ring: {seeded} minutes", file=sys.stderr, flush=True)
    stream = build_stream()

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            process_bars(state, pending["bars"], root, mode, day, window, snapshots)
            pending["bars"] = []
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()


def run_sharded_capture(  # pragma: no cover (live multiprocess loop; logic is unit-tested in sharded_capture)
    symbols: list[str], root: str, mode: str, n_shards: int | None = None,
    window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None, max_minutes: int | None = None,
    snapshots: dict | None = None,
) -> None:
    """Production scale-out: ONE reader owns the websocket and routes each completed minute to N
    persistent worker processes by hash(symbol); the reader runs the universe-wide reduce itself. Each
    worker computes the SAME group code on its shard (byte-identical to single-process — proven in
    tests/test_fp_sharding.py), and writes only its own symbols (partition-disjoint, no DB contention).

    ``snapshots`` overrides the slowly-changing reference/daily frames (else they are loaded from the DB /
    Alpaca) — used by the streaming benchmark to run fully standalone with synthetic snapshots."""
    # ~4 cores per shard: fewer, fatter, multi-threaded shards minimize the per-minute critical path (less
    # process-concurrency contention). The optimum moved toward fewer shards as the declarative batching cut
    # per-shard memory traffic. Measured at 10k/32-cores (compute-only p99): 10 shards=661ms, 8=617ms (best),
    # 6=696ms. (Earlier, pre-batching: 30=1929, 16=1720, 10=1551ms.)
    n_shards = n_shards or max(1, (os.cpu_count() or 8) // 4)
    if snapshots is None:
        snapshots = {"reference": load_reference()}
        if day is not None:
            snapshots["daily"] = backfill_daily(day, symbols)
            # Pin cross_sectional_rank to the day's FIXED in-universe set (gap #3): the reduce/gather phase
            # ranks ONLY within this membership, so live ranks over the SAME set backfill does — not the
            # ad-hoc "whoever printed this minute" set, which swings wildly and breaks live↔backfill parity.
            snapshots["universe"] = load_universe(day)
            # EDGAR session snapshot (filings event store, 370d lookback); the per-minute
            # available_at<=minute gate inside the edgar group makes it point-in-time → backfill==live.
            snapshots["filings"] = load_filings(day)
    # Pin each worker's polars to a slice of the cores. Without this every one of the N spawned workers
    # defaults to a full all-core rayon pool, so N workers x C cores = N*C threads thrash C cores (measured:
    # ~20x slower per shard). One thread per shard when n_shards ~= cores; the spawned children inherit this
    # env at import. (The reader keeps its already-initialized default pool for the universe-wide reduce.)
    threads_per_worker = max(1, (os.cpu_count() or n_shards) // n_shards)
    os.environ["POLARS_MAX_THREADS"] = str(threads_per_worker)

    # SPAWN, not fork: the reader process has already initialized polars' rayon threadpool (building the
    # reference/daily snapshots), and forking a process with a live threadpool deadlocks the child on its
    # first parallel polars op. Spawned workers get a fresh interpreter + fresh threadpool — no inheritance.
    ctx = mp.get_context("spawn")
    queues = [ctx.Queue() for _ in range(n_shards)]
    # Each shard's owned tickers (+ the replicated index ETFs) — handed to the worker so it can warm-start
    # its ring from those symbols' settled bars when FP_WARM_START=1 (CRITICAL-2; inert otherwise).
    shard_symbols = [
        [s for s in symbols if shard_of(s, n_shards) == i] + list(INDEX_SYMBOLS)
        for i in range(n_shards)
    ]
    workers = [
        ctx.Process(
            target=worker_main,
            args=(i, n_shards, queues[i], root, mode, window, day,
                  _shard_snapshots(snapshots, symbols, i, n_shards), shard_symbols[i]),
            daemon=True,
        )
        for i in range(n_shards)
    ]
    for worker in workers:
        worker.start()

    reduce_state = CaptureState()
    if warm_start_enabled() and day:
        # The reader's universe-wide reduce buffer is warm-started too (projected + depth-capped exactly as
        # process_reduce keeps it live), so the gather-phase reduce groups also start with full lookback.
        reduce_bars = backfill_bars(day, symbols)
        reduce_minutes = warm_start_ring(
            reduce_state, reduce_bars, depth=reduce_buffer_minutes(window),
            project_columns=reduce_buffer_columns(),
        )
        print(f"[reader] warm-started reduce ring: {reduce_minutes} minutes", file=sys.stderr, flush=True)
    # arrival       = time.time() of the minute's FIRST bar (end-to-end / delivery-inclusive anchor).
    # last_arrival  = time.time() of the minute's LAST bar (pure-compute anchor; max over the minute).
    # symbol_arrivals = per-symbol time.time() of that symbol's bar (drill-down: which tickers are slow).
    pending: dict = {"minute": None, "bars": [], "trades": [], "quotes": [], "done": 0, "arrival": 0.0,
                     "last_arrival": 0.0, "symbol_arrivals": {}}
    # The reader NO LONGER aggregates ticks — it forwards each shard its RAW trades/quotes and the WORKER
    # aggregates its own shard's ticks (threaded TickState per worker). This distributes the tick firehose
    # across the workers instead of the single reader buffering+aggregating all of it inline, so it scales
    # past the old reader-side cap. trade_buf/quote_buf bucket raw tick DICTS by minute until that minute's
    # bars are dispatched (then routed by hash(symbol), same as the bars).
    tick_syms = set(tick_symbols(symbols))
    trade_buf: dict = {}
    quote_buf: dict = {}
    stream = build_stream()

    # OPT-IN raw market-data streams (md:) — a SEPARATE channel from the feature-vector bus. Both flags
    # are checked ONCE here (cheap booleans on the hot path, no per-message string parsing) and the
    # publisher is constructed only when something is enabled, so capture pays ZERO overhead when off.
    md_on = md_publish_enabled()
    ticks_on = tick_publish_enabled()
    md_firehose_syms = md_tick_symbols(tick_syms) if ticks_on else set()
    md_publisher = MarketDataPublisher() if (md_on or ticks_on) else None
    if md_on:
        print("[reader] md: per-minute raw streams ON (FP_PUBLISH_MD=1)", file=sys.stderr, flush=True)
    if ticks_on:
        print(f"[reader] md: tick firehose ON for {len(md_firehose_syms)} symbols (FP_PUBLISH_TICKS=1)",
              file=sys.stderr, flush=True)

    bench_reader = _reader_bench_path(root)  # FP_BENCH_LOG: time the single-threaded reader (route+reduce)

    def dispatch(bars: list[dict], first_arrival: float, last_arrival: float,
                 symbol_arrivals: dict[str, float], minute) -> None:  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        # Pull THIS minute's raw tick dicts and drop stale buckets; the reader forwards them un-aggregated.
        minute_trades = trade_buf.pop(minute, [])
        minute_quotes = quote_buf.pop(minute, [])
        for stale in [m for m in trade_buf if m < minute]:
            del trade_buf[stale]
        for stale in [m for m in quote_buf if m < minute]:
            del quote_buf[stale]
        # Route ticks by hash(symbol) % n_shards — the SAME routing as the bars — so each symbol's ticks
        # land on the worker that owns its bars (index ETFs replicated to every shard, mirroring the bars).
        routed_trades = route_ticks(minute_trades, n_shards) if minute_trades else None
        routed_quotes = route_ticks(minute_quotes, n_shards) if minute_quotes else None
        for shard_id, shard_bars in enumerate(route_minute(bars, n_shards)):
            if shard_bars:
                # (first_arrival, last_arrival, symbol_arrivals, minute, bars, trades, quotes) — the worker
                # aggregates the ticks locally + records the latency metrics off these wall-clock stamps
                # (time.time(), comparable across the reader/worker processes; perf_counter is not).
                shard_symbol_arrivals = {bar["S"]: symbol_arrivals[bar["S"]] for bar in shard_bars}
                shard_trades = routed_trades[shard_id] if routed_trades is not None else []
                shard_quotes = routed_quotes[shard_id] if routed_quotes is not None else []
                queues[shard_id].put((first_arrival, last_arrival, shard_symbol_arrivals, minute,
                                      shard_bars, shard_trades, shard_quotes))
        # OPT-IN per-minute raw market-data publish (md:). FAULT-ISOLATED: a Redis error logs a warning
        # and continues — it must NEVER stall or crash the capture hot path. ~1 publish/symbol/min,
        # pipelined into one round-trip.
        if md_publisher is not None and md_on:
            trades_by_symbol = _group_ticks_by_symbol(minute_trades)
            quotes_by_symbol = _group_ticks_by_symbol(minute_quotes)
            try:
                md_publisher.publish_minute(bars, trades_by_symbol, quotes_by_symbol, minute)
            except redis.exceptions.RedisError as error:
                logger.warning("md: per-minute publish failed (minute=%s): %s", minute, error)
        # gather: universe-wide reduces (cross_sectional_rank + breadth) over ALL symbols. Pass the reader's
        # FULL snapshots (reference/daily) so breadth's sector + multi-day horizons see the whole universe.
        # Time it (perf_counter, in-process) for feature_gather_seconds — the "+ gather" half of bet-latency.
        gather_start = time.perf_counter()
        process_reduce(reduce_state, bars, root, mode, day, window, snapshots=snapshots)
        metrics.record_gather(time.perf_counter() - gather_start)
        if bench_reader is not None:
            with bench_reader.open("a") as handle:
                handle.write(json.dumps({"minute": max(bar["t"] for bar in bars),
                                         "ms": (time.perf_counter() - start) * 1000.0}) + "\n")

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            dispatch(pending["bars"], pending["arrival"], pending["last_arrival"],
                     pending["symbol_arrivals"], pending["minute"])
            pending["bars"] = []
            pending["symbol_arrivals"] = {}
            pending["done"] += 1
            if max_minutes is not None and pending["done"] >= max_minutes:
                if os.environ.get("FP_BENCH_LOG"):
                    print(f"[reader] {pending['done']} minutes dispatched; sending sentinels + stopping",
                          file=sys.stderr, flush=True)
                for queue in queues:
                    queue.put(None)  # shutdown sentinel: workers drain their queue then exit
                await stream.stop_ws()
                return
        now = time.time()
        if not pending["bars"]:
            # Wall-clock the instant THIS minute's first bar landed off the websocket — the end-to-end
            # (delivery-inclusive) anchor for feature_vector_latency_seconds.
            pending["arrival"] = now
        # Every bar updates last_arrival (kept as the max) — the LAST bar of the minute is the pure-compute
        # anchor for feature_assemble_seconds (Alpaca has finished delivering by then). Per-symbol arrival
        # feeds the drill-down (which tickers were delivered late / slow in our pipeline).
        pending["last_arrival"] = now
        pending["symbol_arrivals"][bar.symbol] = now
        metrics.BARS_INGESTED.inc()
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    async def on_trade(trade) -> None:  # type: ignore[no-untyped-def]
        metrics.TRADES_INGESTED.inc()
        minute = trade.timestamp.replace(second=0, microsecond=0)
        record = {"S": trade.symbol, "p": float(trade.price), "s": float(trade.size),
                  "ts_epoch": trade.timestamp.timestamp()}
        # Buffer the RAW trade dict (the reader forwards it un-aggregated; the worker aggregates per shard).
        trade_buf.setdefault(minute, []).append(record)
        # OPT-IN tick firehose (md:tick_trades:<symbol>). FAULT-ISOLATED + symbol-gated; bounded MAXLEN.
        if md_publisher is not None and ticks_on and trade.symbol in md_firehose_syms:
            try:
                md_publisher.publish_tick(trade.symbol, minute, "trades", record)
            except redis.exceptions.RedisError as error:
                logger.warning("md: tick_trades publish failed (%s): %s", trade.symbol, error)

    async def on_quote(quote) -> None:  # type: ignore[no-untyped-def]
        metrics.QUOTES_INGESTED.inc()
        minute = quote.timestamp.replace(second=0, microsecond=0)
        record = {"S": quote.symbol, "bp": float(quote.bid_price), "ap": float(quote.ask_price),
                  "bs": float(quote.bid_size), "as": float(quote.ask_size),
                  "ts_epoch": quote.timestamp.timestamp()}
        quote_buf.setdefault(minute, []).append(record)
        # OPT-IN tick firehose (md:tick_quotes:<symbol>). FAULT-ISOLATED + symbol-gated; bounded MAXLEN.
        if md_publisher is not None and ticks_on and quote.symbol in md_firehose_syms:
            try:
                md_publisher.publish_tick(quote.symbol, minute, "quotes", record)
            except redis.exceptions.RedisError as error:
                logger.warning("md: tick_quotes publish failed (%s): %s", quote.symbol, error)

    # The reader exposes its own /metrics (ingestion counters) — workers own 9201..9208, reader owns 9200.
    metrics.start_metrics_server(int(os.environ.get("READER_METRICS_PORT", "9200")))
    stream.subscribe_bars(on_bar, *symbols)
    if tick_syms:
        stream.subscribe_trades(on_trade, *tick_syms)
        stream.subscribe_quotes(on_quote, *tick_syms)
        print(f"[reader] tick streaming ON: trades+quotes for {len(tick_syms)} symbols", file=sys.stderr, flush=True)
    stream.run()
    if os.environ.get("FP_BENCH_LOG"):
        print("[reader] stream.run() returned; joining workers", file=sys.stderr, flush=True)
    for worker in workers:
        worker.join(timeout=300)  # bounded mode: let workers drain their queued minutes before exit


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.real_capture <sym,sym> <root> <real|mock> [day] [--sharded]")
    symbols, root, mode = sys.argv[1].split(","), sys.argv[2], sys.argv[3]
    day = sys.argv[4] if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else None
    if "--sharded" in sys.argv:
        run_sharded_capture(symbols, root, mode, day=day)
    else:
        run_capture(symbols, root, mode, day=day)


if __name__ == "__main__":
    main()
