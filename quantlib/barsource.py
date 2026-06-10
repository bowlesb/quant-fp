"""Shared historical bar fetch+store, used by the one-shot backfiller and the
continuous backfill-manager. Split+dividend adjusted; upserts so a re-fetch
self-corrects earlier raw/partial data (idempotent and resumable from the DB).
"""
import time

import psycopg
from alpaca.data.enums import Adjustment
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

CHUNK = 100

_UPSERT = """
INSERT INTO bars_1m
    (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO UPDATE SET
    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
    volume=EXCLUDED.volume, vwap=EXCLUDED.vwap, trade_count=EXCLUDED.trade_count,
    ingested_at=now()
"""


def fetch_and_store_bars(
    data_client: StockHistoricalDataClient,
    conn: psycopg.Connection,
    symbols: list[str],
    start,
    end,
    pause_seconds: float = 0.3,
) -> int:
    """Fetch adjusted 1-min bars for symbols over [start, end] and upsert them as
    source='backfill'. Returns the number of bar rows written."""
    total = 0
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        request = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
            start=start, end=end, adjustment=Adjustment.ALL,
        )
        barset = data_client.get_stock_bars(request)
        rows = [
            (symbol, bar.timestamp, bar.open, bar.high, bar.low, bar.close,
             int(bar.volume), bar.vwap, bar.trade_count)
            for symbol, bars in barset.data.items()
            for bar in bars
        ]
        if rows:
            with conn.cursor() as cur:
                cur.executemany(_UPSERT, rows)
            total += len(rows)
        time.sleep(pause_seconds)
    return total
