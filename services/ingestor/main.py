"""Ingestor: Alpaca SIP websocket -> TimescaleDB.

Streams bars, trades, and quotes for the symbol set. Trades and quotes are
buffered per symbol per minute and, when the minute's bar arrives (signaling the
minute has closed), aggregated through quantlib.aggregates — the SAME functions
the historical backfiller uses — and written to trade_agg_1m / quote_agg_1m.
Raw trades are bulk-inserted into trades_raw (30-day rolling) for debugging and
developing new aggregates.

Single-threaded asyncio, so the shared buffers need no locks.
"""
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingestor")

DEFAULT_SYMBOLS = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,SPY,QQQ,JPM"
# Trades/quotes are far higher volume than bars; subscribe them only for this
# subset to keep a single ingestor process healthy. Bars are streamed for the
# whole universe. Expand TRADE_QUOTE_SYMBOLS as we sharded/optimize ingestion.
TQ_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("TRADE_QUOTE_SYMBOLS", DEFAULT_SYMBOLS).split(",")
    if s.strip()
]
FEED = os.environ.get("ALPACA_DATA_FEED", "sip").lower()

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

BAR_SQL = """
INSERT INTO bars_1m (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'stream')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""
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

# Per symbol -> per minute-epoch -> buffered ticks. defaultdict avoids key checks.
trades_buf: dict[str, dict[int, list[TradeTick]]] = defaultdict(lambda: defaultdict(list))
quotes_buf: dict[str, dict[int, list[QuoteTick]]] = defaultdict(lambda: defaultdict(list))
raw_buf: dict[str, dict[int, list[tuple]]] = defaultdict(lambda: defaultdict(list))
tick_state: dict[str, TickState] = defaultdict(TickState)

_conn: psycopg.Connection | None = None
_bar_count = 0


def get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(**DB_KWARGS, autocommit=True)
    return _conn


def load_bar_symbols() -> list[str]:
    """Bars are streamed for the whole current universe; fall back to the default
    set if the universe hasn't been built yet."""
    try:
        with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM universe_membership WHERE trade_date = "
                "(SELECT max(trade_date) FROM universe_membership) ORDER BY symbol"
            )
            symbols = [row[0] for row in cur.fetchall()]
        if symbols:
            return symbols
    except psycopg.Error as exc:
        logger.warning("could not load universe, using default symbols: %s", exc)
    return [s.strip().upper() for s in DEFAULT_SYMBOLS.split(",")]


def flush_minute(conn: psycopg.Connection, symbol: str, minute_epoch: int) -> None:
    """Aggregate and persist one closed minute for one symbol, then drop it."""
    minute_ts = datetime.fromtimestamp(minute_epoch, tz=timezone.utc)
    trades = trades_buf[symbol].pop(minute_epoch, [])
    quotes = quotes_buf[symbol].pop(minute_epoch, [])
    raw = raw_buf[symbol].pop(minute_epoch, [])

    with conn.cursor() as cur:
        if trades:
            agg = aggregate_trades(trades, tick_state[symbol])
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


def flush_through(conn: psycopg.Connection, symbol: str, minute_epoch: int) -> None:
    """Flush every buffered minute <= minute_epoch in time order (so the trade
    tick-rule state is threaded correctly even if a minute had no bar)."""
    pending = sorted(
        set(trades_buf[symbol]) | set(quotes_buf[symbol]) | set(raw_buf[symbol])
    )
    for minute in pending:
        if minute > minute_epoch:
            break
        flush_minute(conn, symbol, minute)


async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
    global _conn, _bar_count
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                BAR_SQL,
                (
                    bar.symbol, bar.timestamp, bar.open, bar.high, bar.low,
                    bar.close, int(bar.volume), bar.vwap, bar.trade_count,
                ),
            )
        flush_through(conn, bar.symbol, bucket_minute(bar.timestamp.timestamp()))
        _bar_count += 1
        if _bar_count % 10 == 0:
            logger.info("bars stored: %d (latest %s %s)", _bar_count, bar.symbol, bar.timestamp.isoformat())
    except psycopg.Error as exc:
        logger.error("DB error on bar %s: %s", bar.symbol, exc)
        _conn = None


async def on_trade(trade) -> None:  # type: ignore[no-untyped-def]
    minute = bucket_minute(trade.timestamp.timestamp())
    trades_buf[trade.symbol][minute].append(
        TradeTick(trade.timestamp.timestamp(), float(trade.price), float(trade.size))
    )
    raw_buf[trade.symbol][minute].append(
        (
            trade.symbol, trade.timestamp, float(trade.price), float(trade.size),
            getattr(trade, "exchange", None),
            ",".join(trade.conditions) if getattr(trade, "conditions", None) else None,
            getattr(trade, "tape", None),
        )
    )


async def on_quote(quote) -> None:  # type: ignore[no-untyped-def]
    minute = bucket_minute(quote.timestamp.timestamp())
    quotes_buf[quote.symbol][minute].append(
        QuoteTick(
            quote.timestamp.timestamp(),
            float(quote.bid_price), float(quote.ask_price),
            float(quote.bid_size), float(quote.ask_size),
        )
    )


def main() -> None:
    bar_symbols = load_bar_symbols()
    logger.info(
        "ingestor starting: bars for %d symbols, trades/quotes for %d, feed=%s",
        len(bar_symbols), len(TQ_SYMBOLS), FEED,
    )
    feed_enum = DataFeed.SIP if FEED == "sip" else DataFeed.IEX
    stream = StockDataStream(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], feed=feed_enum
    )
    stream.subscribe_bars(on_bar, *bar_symbols)
    stream.subscribe_trades(on_trade, *TQ_SYMBOLS)
    stream.subscribe_quotes(on_quote, *TQ_SYMBOLS)
    stream.run()


if __name__ == "__main__":
    main()
