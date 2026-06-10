"""Scheduler: standing jobs that run without a human.

Phase 0 job: compute per-symbol data-quality coverage (received vs expected
1-minute bars) for the current or latest trading session and upsert into
data_quality_daily, so the dashboard can show ingestion health. Later phases add
streamed-vs-REST verification, gap repair, weekly retrain, and NAS backups.
"""
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("scheduler")

NY = ZoneInfo("America/New_York")
LOOP_SECONDS = int(os.environ.get("SCHED_LOOP_SECONDS", "1800"))
SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get(
        "SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,SPY,QQQ,JPM"
    ).split(",")
    if s.strip()
]

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

trading = TradingClient(
    os.environ["ALPACA_KEY_ID"],
    os.environ["ALPACA_SECRET_KEY"],
    paper=(os.environ.get("MODE", "paper") == "paper"),
)


def coerce_time(value: object) -> dtime:
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, dtime):
        return value
    text = str(value)
    if " " in text:                      # e.g. "2026-06-03 09:30:00"
        text = text.split(" ")[-1]
    parts = text.split(":")
    return dtime(int(parts[0]), int(parts[1]))


def coerce_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).split(" ")[0])


def session_window(session_date: date, open_t: object, close_t: object) -> tuple[datetime, datetime]:
    open_utc = datetime.combine(session_date, coerce_time(open_t), tzinfo=NY).astimezone(timezone.utc)
    close_utc = datetime.combine(session_date, coerce_time(close_t), tzinfo=NY).astimezone(timezone.utc)
    return open_utc, close_utc


def pick_session() -> tuple[date, datetime, datetime] | None:
    """Current session if in progress (window capped at now), else latest complete."""
    now = datetime.now(timezone.utc)
    calendar = trading.get_calendar(
        GetCalendarRequest(start=(now - timedelta(days=7)).date(), end=now.date())
    )
    chosen = None
    for session in calendar:
        session_date = coerce_date(session.date)
        open_utc, close_utc = session_window(session_date, session.open, session.close)
        if open_utc <= now:
            chosen = (session_date, open_utc, min(close_utc, now))
    return chosen


def write_coverage(conn: psycopg.Connection, session_date: date, open_utc: datetime, end_utc: datetime) -> list[tuple[str, int, int]]:
    expected = max(1, int((end_utc - open_utc).total_seconds() // 60))
    results = []
    with conn.cursor() as cur:
        for symbol in SYMBOLS:
            cur.execute(
                """
                SELECT count(DISTINCT date_trunc('minute', ts))
                FROM bars_1m
                WHERE symbol = %s AND ts >= %s AND ts < %s
                """,
                (symbol, open_utc, end_utc),
            )
            row = cur.fetchone()
            received = int(row[0]) if row else 0
            cur.execute(
                """
                INSERT INTO data_quality_daily
                    (trade_date, symbol, expected_minutes, received_minutes, repaired_minutes)
                VALUES (%s,%s,%s,%s,0)
                ON CONFLICT (trade_date, symbol) DO UPDATE
                SET expected_minutes = EXCLUDED.expected_minutes,
                    received_minutes = EXCLUDED.received_minutes
                """,
                (session_date, symbol, expected, received),
            )
            results.append((symbol, received, expected))
    return results


def run_daily_job() -> None:
    session = pick_session()
    if session is None:
        logger.info("no trading session in the last 7 days; nothing to report")
        return
    session_date, open_utc, end_utc = session
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        results = write_coverage(conn, session_date, open_utc, end_utc)
    expected = results[0][2] if results else 0
    worst = min((r[1] for r in results), default=0)
    mean_cov = (
        100.0 * sum(r[1] for r in results) / (len(results) * expected)
        if results and expected
        else 0.0
    )
    logger.info(
        "coverage %s: %d symbols, expected=%d min, mean=%.1f%%, worst symbol=%d min",
        session_date,
        len(results),
        expected,
        mean_cov,
        worst,
    )


def main() -> None:
    logger.info("scheduler starting: loop=%ds, %d symbols", LOOP_SECONDS, len(SYMBOLS))
    while True:
        try:
            run_daily_job()
        except (psycopg.Error, ValueError, KeyError) as exc:
            logger.error("daily job error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
