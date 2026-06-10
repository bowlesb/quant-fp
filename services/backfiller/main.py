"""Backfiller + streamed-vs-REST validation.

`backfill-bars`  : pull historical 1-minute bars via REST into bars_1m as
                   source='backfill' (append-only; never overwrites stream rows).
`validate-bars`  : compare overlapping (symbol, ts) bars between source='stream'
                   and source='backfill' and report the OHLCV match rate — the
                   Phase 1 gate that proves the live feed equals historical REST.

Trade/quote-aggregate backfill (through the same quantlib functions the ingestor
uses) is the next step; bars parity is validated first.

Usage: python main.py <command>   (config via env, see below)
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfiller")

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

data_client = StockHistoricalDataClient(
    os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
)

CHUNK = 200
BAR_SQL = """
INSERT INTO bars_1m (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""


def universe_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM universe_membership WHERE trade_date = "
            "(SELECT max(trade_date) FROM universe_membership) ORDER BY symbol"
        )
        return [row[0] for row in cur.fetchall()]


def resolve_symbols(conn: psycopg.Connection) -> list[str]:
    env = os.environ.get("BACKFILL_SYMBOLS", "").strip()
    if env.lower() == "universe":
        return universe_symbols(conn)
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "JPM"]


def backfill_bars() -> None:
    start = datetime.fromisoformat(
        os.environ.get("BACKFILL_START", datetime.now(timezone.utc).date().isoformat())
    ).replace(tzinfo=timezone.utc)
    end_env = os.environ.get("BACKFILL_END")
    end = (
        datetime.fromisoformat(end_env).replace(tzinfo=timezone.utc)
        if end_env
        else datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = resolve_symbols(conn)
        logger.info("backfilling bars for %d symbols, %s .. %s", len(symbols), start, end)
        total = 0
        for i in range(0, len(symbols), CHUNK):
            chunk = symbols[i : i + CHUNK]
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Minute, start=start, end=end
            )
            barset = data_client.get_stock_bars(request)
            with conn.cursor() as cur:
                for symbol, bars in barset.data.items():
                    for bar in bars:
                        cur.execute(
                            BAR_SQL,
                            (
                                symbol, bar.timestamp, bar.open, bar.high, bar.low,
                                bar.close, int(bar.volume), bar.vwap, bar.trade_count,
                            ),
                        )
                        total += 1
            logger.info("backfilled through %d/%d symbols (%d bars)", min(i + CHUNK, len(symbols)), len(symbols), total)
        logger.info("backfill complete: %d bars inserted (source=backfill)", total)


def validate_bars() -> None:
    """Compare stream vs backfill for overlapping (symbol, ts). OHLCV must match
    within tolerance. This is the Phase 1 streamed-vs-REST gate."""
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH joined AS (
                SELECT s.symbol, s.ts,
                       (abs(s.open - b.open)   <= 0.01
                    AND abs(s.high - b.high)   <= 0.01
                    AND abs(s.low  - b.low)    <= 0.01
                    AND abs(s.close - b.close) <= 0.01) AS ohlc_match,
                       (s.volume = b.volume)            AS vol_match
                FROM bars_1m s
                JOIN bars_1m b ON b.symbol = s.symbol AND b.ts = s.ts
                WHERE s.source = 'stream' AND b.source = 'backfill'
            )
            SELECT count(*),
                   count(*) FILTER (WHERE ohlc_match),
                   count(*) FILTER (WHERE ohlc_match AND vol_match)
            FROM joined
            """
        )
        total, ohlc_ok, full_ok = cur.fetchone()
        if total == 0:
            logger.warning("no overlapping stream/backfill bars to validate")
            return
        logger.info(
            "validation: %d overlapping bars | OHLC match %.3f%% | OHLC+volume match %.3f%%",
            total, 100.0 * ohlc_ok / total, 100.0 * full_ok / total,
        )
        cur.execute(
            """
            SELECT s.symbol, s.ts, s.close, b.close, s.volume, b.volume
            FROM bars_1m s JOIN bars_1m b ON b.symbol=s.symbol AND b.ts=s.ts
            WHERE s.source='stream' AND b.source='backfill'
              AND (abs(s.close-b.close) > 0.01 OR s.volume <> b.volume)
            ORDER BY s.ts LIMIT 5
            """
        )
        for row in cur.fetchall():
            logger.info("  mismatch %s %s stream(close=%s vol=%s) backfill(close=%s vol=%s)",
                        row[0], row[1], row[2], row[4], row[3], row[5])


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "backfill-bars"
    if command == "backfill-bars":
        backfill_bars()
    elif command == "validate-bars":
        validate_bars()
    else:
        raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    main()
