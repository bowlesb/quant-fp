"""Ingestor: Alpaca stock-data websocket -> bars_1m in TimescaleDB.

Phase 0 scope: subscribe to 1-minute bars for a small symbol set and persist
them as source='stream'. The alpaca-py StockDataStream handles auth and
reconnection. DB writes are synchronous per bar — fine at this volume; the
full-universe build will batch async (see ARCHITECTURE.md).
"""
import logging
import os

import psycopg
from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("ingestor")

SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get(
        "SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,SPY,QQQ,JPM"
    ).split(",")
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

INSERT_SQL = """
INSERT INTO bars_1m
    (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'stream')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""

_conn: psycopg.Connection | None = None
_bar_count = 0


def get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(**DB_KWARGS, autocommit=True)
    return _conn


async def on_bar(bar: Bar) -> None:
    global _conn, _bar_count
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                INSERT_SQL,
                (
                    bar.symbol,
                    bar.timestamp,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    int(bar.volume),
                    bar.vwap,
                    bar.trade_count,
                ),
            )
        _bar_count += 1
        logger.info(
            "bar %s %s close=%.4f vol=%d (total bars stored: %d)",
            bar.symbol,
            bar.timestamp.isoformat(),
            bar.close,
            int(bar.volume),
            _bar_count,
        )
    except psycopg.Error as exc:
        logger.error("DB error writing bar for %s: %s", bar.symbol, exc)
        _conn = None


def main() -> None:
    logger.info(
        "ingestor starting: %d symbols, feed=%s, symbols=%s",
        len(SYMBOLS),
        FEED,
        ",".join(SYMBOLS),
    )
    feed_enum = DataFeed.SIP if FEED == "sip" else DataFeed.IEX
    stream = StockDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=feed_enum,
    )
    stream.subscribe_bars(on_bar, *SYMBOLS)
    stream.run()


if __name__ == "__main__":
    main()
