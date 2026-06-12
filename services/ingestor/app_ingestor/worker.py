"""Aggregation worker: owns one symbol shard, does the CPU-heavy per-minute
quantlib aggregation + DB writes for it.

This is where the parity cornerstone lives: the buffer/flush/threaded-tick_state
logic is byte-for-byte the same algorithm as the pre-shard single-process ingestor
(and the historical backfiller), just scoped to this worker's shard. A bar arriving
for a symbol is the minute-close signal that flushes that symbol's buffered ticks,
exactly as before — bars stream for the whole universe, so every OFI symbol's
minute close still arrives here.

One worker == one OS process == one CPU core's worth of aggregation. Sharding by
symbol means a worker's `tick_state[symbol]` is never touched by another process,
so the cross-minute tick rule stays correct without any locking.
"""
import logging
import os
import queue as queue_mod
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Queue
from multiprocessing.synchronize import Event as EventType

import psycopg

from app_ingestor.coverage import ShardCoverage
from app_ingestor.shard import (
    KIND_BAR,
    KIND_QUOTE,
    KIND_TRADE,
    BarMsg,
    QuoteMsg,
    TradeMsg,
)
from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)

logger = logging.getLogger("ingestor.worker")

TRADE_AGG_SQL = """
INSERT INTO trade_agg_1m
    (symbol, ts, signed_volume, buy_volume, sell_volume, large_print_cnt,
     trade_intensity, median_size, p95_size, n_trades, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'stream')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""
QUOTE_AGG_SQL = """
INSERT INTO quote_agg_1m
    (symbol, ts, mean_spread_bps, median_spread_bps, mean_bid_size, mean_ask_size,
     quote_imbalance, n_quotes, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'stream')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""
RAW_SQL = """
INSERT INTO trades_raw (symbol, ts, price, size, exchange, conditions, tape)
VALUES (%s,%s,%s,%s,%s,%s,%s)
"""


class Worker:
    """Owns one shard. Buffers ticks per symbol per minute; a bar flushes that
    symbol's closed minute(s) through the quantlib aggregates into the DB."""

    def __init__(self, shard_id: int, db_kwargs: dict[str, str | int]) -> None:
        self.shard_id = shard_id
        self.db_kwargs = db_kwargs
        self.trades_buf: dict[str, dict[int, list[TradeTick]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.quotes_buf: dict[str, dict[int, list[QuoteTick]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.raw_buf: dict[str, dict[int, list[tuple]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.tick_state: dict[str, TickState] = defaultdict(TickState)
        self._conn: psycopg.Connection | None = None

    def get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(**self.db_kwargs, autocommit=True)
        return self._conn

    def on_trade(self, msg: TradeMsg) -> None:
        minute = bucket_minute(msg.ts_epoch)
        self.trades_buf[msg.symbol][minute].append(
            TradeTick(msg.ts_epoch, msg.price, msg.size)
        )
        ts = datetime.fromtimestamp(msg.ts_epoch, tz=timezone.utc)
        self.raw_buf[msg.symbol][minute].append(
            (msg.symbol, ts, msg.price, msg.size, msg.exchange, msg.conditions, msg.tape)
        )

    def on_quote(self, msg: QuoteMsg) -> None:
        minute = bucket_minute(msg.ts_epoch)
        self.quotes_buf[msg.symbol][minute].append(
            QuoteTick(msg.ts_epoch, msg.bid, msg.ask, msg.bid_size, msg.ask_size)
        )

    def flush_minute(self, conn: psycopg.Connection, symbol: str, minute_epoch: int) -> None:
        minute_ts = datetime.fromtimestamp(minute_epoch, tz=timezone.utc)
        trades = self.trades_buf[symbol].pop(minute_epoch, [])
        quotes = self.quotes_buf[symbol].pop(minute_epoch, [])
        raw = self.raw_buf[symbol].pop(minute_epoch, [])

        with conn.cursor() as cur:
            if trades:
                agg = aggregate_trades(trades, self.tick_state[symbol])
                cur.execute(
                    TRADE_AGG_SQL,
                    (
                        symbol, minute_ts, agg.signed_volume, agg.buy_volume,
                        agg.sell_volume, agg.large_print_cnt, agg.trade_intensity,
                        agg.median_size, agg.p95_size, agg.n_trades,
                    ),
                )
            if quotes:
                qagg = aggregate_quotes(quotes)
                cur.execute(
                    QUOTE_AGG_SQL,
                    (
                        symbol, minute_ts, qagg.mean_spread_bps, qagg.median_spread_bps,
                        qagg.mean_bid_size, qagg.mean_ask_size, qagg.quote_imbalance,
                        qagg.n_quotes,
                    ),
                )
            if raw:
                cur.executemany(RAW_SQL, raw)

    def flush_through(self, conn: psycopg.Connection, symbol: str, minute_epoch: int) -> None:
        """Flush every buffered minute <= minute_epoch in time order so the trade
        tick-rule state threads correctly even across a minute that had no bar."""
        pending = sorted(
            set(self.trades_buf[symbol])
            | set(self.quotes_buf[symbol])
            | set(self.raw_buf[symbol])
        )
        for minute in pending:
            if minute > minute_epoch:
                break
            self.flush_minute(conn, symbol, minute)

    def on_bar(self, msg: BarMsg, coverage: ShardCoverage) -> None:
        conn = self.get_conn()
        minute_epoch = bucket_minute(msg.ts_epoch)
        had_trade = bool(self.trades_buf[msg.symbol].get(minute_epoch))
        self.flush_through(conn, msg.symbol, minute_epoch)
        coverage.record_bar(msg.symbol, minute_epoch, had_trade)


def run_worker(
    shard_id: int,
    in_queue: "Queue",
    db_kwargs: dict[str, str | int],
    expected_symbols: list[str],
    stop: EventType,
) -> None:
    """Worker process entrypoint. Consumes messages until `stop` is set; emits a
    coverage gauge each minute for the shard's expected symbols."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    metrics_port = int(os.environ["WORKER_METRICS_BASE_PORT"]) + shard_id
    worker = Worker(shard_id, db_kwargs)
    coverage = ShardCoverage(shard_id, set(expected_symbols), metrics_port)
    coverage.heartbeat()
    logger.info(
        "worker %d up: %d expected symbols, metrics :%d",
        shard_id, len(expected_symbols), metrics_port,
    )

    while not stop.is_set():
        # Bump liveness every iteration (incl. the idle path below) so a wedged
        # worker is caught as a stale heartbeat even when no messages arrive.
        coverage.heartbeat()
        try:
            coverage.set_queue_depth(in_queue.qsize())
        except NotImplementedError:
            pass  # qsize() is unsupported on some platforms (macOS); skip the gauge
        try:
            kind, payload = in_queue.get(timeout=1.0)
        except queue_mod.Empty:
            continue
        if kind == KIND_TRADE:
            worker.on_trade(payload)
        elif kind == KIND_QUOTE:
            worker.on_quote(payload)
        elif kind == KIND_BAR:
            worker.on_bar(payload, coverage)

    flush_pending_on_stop(worker)


def flush_pending_on_stop(worker: Worker) -> None:
    """On a GRACEFUL stop (SIGTERM / planned deploy), flush every buffered minute so
    a coordinated restart doesn't drop the in-flight minute's aggregates. A crash
    can't run this — that gap is recoverable via the backfill (source='backfill')
    and visible in the coverage gauges; a planned restart should lose nothing."""
    conn = worker.get_conn()
    pending_symbols = (
        set(worker.trades_buf) | set(worker.quotes_buf) | set(worker.raw_buf)
    )
    flushed = 0
    for symbol in pending_symbols:
        minutes = (
            set(worker.trades_buf[symbol])
            | set(worker.quotes_buf[symbol])
            | set(worker.raw_buf[symbol])
        )
        if not minutes:
            continue
        worker.flush_through(conn, symbol, max(minutes))
        flushed += 1
    if flushed:
        logger.info("worker %d flushed %d symbols' buffers on stop", worker.shard_id, flushed)
