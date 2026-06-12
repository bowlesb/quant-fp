"""Reader: owns the single Alpaca websocket; routes ticks to shard workers.

Alpaca allows ONE concurrent market-data websocket per account, so there is exactly
one reader. It does the cheap work only — receive + route — and never aggregates:
a slow aggregation must never back up the websocket receive loop (that's how ticks
get dropped at scale). All CPU-heavy per-minute aggregation happens in the worker
processes, sharded by symbol.

Bars stream for the whole equities universe (+ market-context ETFs). The reader
writes EVERY bar to bars_1m itself (light I/O, one place), and for OFI symbols also
forwards a minute-close BarMsg to the owning worker so it flushes that symbol's
buffered ticks — the same bar-triggered flush as the single-process ingestor, just
delivered across the shard queue.
"""
import logging
import os
from multiprocessing import Queue

import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from app_ingestor.shard import (
    KIND_BAR,
    KIND_QUOTE,
    KIND_TRADE,
    BarMsg,
    QuoteMsg,
    TradeMsg,
    shard_for,
)

logger = logging.getLogger("ingestor.reader")

BAR_SQL = """
INSERT INTO bars_1m (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'stream')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""


class Reader:
    """Holds the websocket callbacks. Trades/quotes route to the owning shard queue;
    bars are persisted here and (for OFI symbols) forwarded as a flush signal."""

    def __init__(
        self,
        queues: list["Queue"],
        ofi_symbols: set[str],
        db_kwargs: dict[str, str | int],
    ) -> None:
        self.queues = queues
        self.n_shards = len(queues)
        self.ofi_symbols = ofi_symbols
        self.db_kwargs = db_kwargs
        self._conn: psycopg.Connection | None = None
        self._bar_count = 0

    def get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(**self.db_kwargs, autocommit=True)
        return self._conn

    async def on_bar(self, bar) -> None:  # type: ignore[no-untyped-def]
        try:
            conn = self.get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    BAR_SQL,
                    (
                        bar.symbol, bar.timestamp, bar.open, bar.high, bar.low,
                        bar.close, int(bar.volume), bar.vwap, bar.trade_count,
                    ),
                )
            self._bar_count += 1
            if self._bar_count % 100 == 0:
                logger.info(
                    "bars stored: %d (latest %s %s)",
                    self._bar_count, bar.symbol, bar.timestamp.isoformat(),
                )
        except psycopg.Error as exc:
            logger.error("DB error on bar %s: %s", bar.symbol, exc)
            self._conn = None

        if bar.symbol in self.ofi_symbols:
            shard = shard_for(bar.symbol, self.n_shards)
            self.queues[shard].put(
                (KIND_BAR, BarMsg(bar.symbol, bar.timestamp.timestamp()))
            )

    async def on_trade(self, trade) -> None:  # type: ignore[no-untyped-def]
        shard = shard_for(trade.symbol, self.n_shards)
        self.queues[shard].put(
            (
                KIND_TRADE,
                TradeMsg(
                    trade.symbol, trade.timestamp.timestamp(),
                    float(trade.price), float(trade.size),
                    getattr(trade, "exchange", None),
                    ",".join(trade.conditions)
                    if getattr(trade, "conditions", None)
                    else None,
                    getattr(trade, "tape", None),
                ),
            )
        )

    async def on_quote(self, quote) -> None:  # type: ignore[no-untyped-def]
        shard = shard_for(quote.symbol, self.n_shards)
        self.queues[shard].put(
            (
                KIND_QUOTE,
                QuoteMsg(
                    quote.symbol, quote.timestamp.timestamp(),
                    float(quote.bid_price), float(quote.ask_price),
                    float(quote.bid_size), float(quote.ask_size),
                ),
            )
        )


def run_reader(
    queues: list["Queue"],
    bar_symbols: list[str],
    ofi_symbols: list[str],
    db_kwargs: dict[str, str | int],
) -> None:
    """Reader process entrypoint: subscribe the single websocket and run it."""
    feed = os.environ.get("ALPACA_DATA_FEED", "sip").lower()
    feed_enum = DataFeed.SIP if feed == "sip" else DataFeed.IEX
    reader = Reader(queues, set(ofi_symbols), db_kwargs)
    logger.info(
        "reader up: bars for %d symbols, trades/quotes for %d, %d shards, feed=%s",
        len(bar_symbols), len(ofi_symbols), len(queues), feed,
    )
    stream = StockDataStream(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], feed=feed_enum
    )
    stream.subscribe_bars(reader.on_bar, *bar_symbols)
    stream.subscribe_trades(reader.on_trade, *ofi_symbols)
    stream.subscribe_quotes(reader.on_quote, *ofi_symbols)
    stream.run()
