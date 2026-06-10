"""Backfill manager: keeps the bar history backfilled to a target depth, on its
own. Walks month windows from (today - target) to now, fetching adjusted bars for
the current universe, oldest-missing first. Resumable via backfill_windows (skips
months already 'done'); the current month is always refreshed. Rate-limited and
idempotent. Raising BACKFILL_TARGET_DAYS (e.g. toward 6 years) just adds older
months to work through — no code change.
"""
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from alpaca.data.historical import StockHistoricalDataClient

from quantlib.barsource import fetch_and_store_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill-manager")

TARGET_DAYS = int(os.environ.get("BACKFILL_TARGET_DAYS", "90"))
IDLE_SECONDS = int(os.environ.get("BACKFILL_IDLE_SECONDS", "3600"))
PARTITION_PAUSE = float(os.environ.get("BACKFILL_PAUSE_SECONDS", "0.3"))

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


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=7)).replace(day=1)


def target_months() -> list[date]:
    today = datetime.now(timezone.utc).date()
    start = month_start(today - timedelta(days=TARGET_DAYS))
    months, cursor = [], start
    while cursor <= today:
        months.append(cursor)
        cursor = next_month(cursor)
    return months


def universe_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM universe_membership WHERE trade_date = "
            "(SELECT max(trade_date) FROM universe_membership) ORDER BY symbol"
        )
        return [row[0] for row in cur.fetchall()]


def done_months(conn: psycopg.Connection) -> set[date]:
    with conn.cursor() as cur:
        cur.execute("SELECT month_start FROM backfill_windows WHERE status='done'")
        return {row[0] for row in cur.fetchall()}


def record_window(conn: psycopg.Connection, m_start: date, status: str, bars: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO backfill_windows (month_start, status, bars, updated_at)
               VALUES (%s,%s,%s, now())
               ON CONFLICT (month_start) DO UPDATE
               SET status=EXCLUDED.status, bars=EXCLUDED.bars, updated_at=now()""",
            (m_start, status, bars),
        )


def run_once() -> bool:
    """Fetch one outstanding month window. Returns True if it did work."""
    now = datetime.now(timezone.utc)
    this_month = month_start(now.date())
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = universe_symbols(conn)
        if not symbols:
            logger.info("no universe yet; idling")
            return False
        done = done_months(conn)
        # Oldest-missing first; the current month is never 'done' (always refresh).
        for m_start in target_months():
            if m_start in done and m_start != this_month:
                continue
            window_start = datetime(m_start.year, m_start.month, 1, tzinfo=timezone.utc)
            window_end = min(
                datetime.combine(next_month(m_start), datetime.min.time(), tzinfo=timezone.utc),
                now - timedelta(minutes=1),
            )
            logger.info("backfilling month %s for %d symbols", m_start, len(symbols))
            bars = fetch_and_store_bars(
                data_client, conn, symbols, window_start, window_end, PARTITION_PAUSE
            )
            status = "partial" if m_start == this_month else "done"
            record_window(conn, m_start, status, bars)
            logger.info("month %s %s: %d bars", m_start, status, bars)
            return True
    return False


_NY = ZoneInfo("America/New_York")


def is_market_hours() -> bool:
    """Roughly RTH (09:30-16:00 ET, Mon-Fri) — when we throttle so the live open
    burst (ingestion + universe scoring) isn't starved of DB I/O."""
    now = datetime.now(timezone.utc).astimezone(_NY)
    return now.weekday() < 5 and dtime(9, 30) <= now.timetz().replace(tzinfo=None) < dtime(16, 0)


def main() -> None:
    logger.info("backfill-manager starting: target=%d days", TARGET_DAYS)
    while True:
        if is_market_hours():
            logger.info("market hours: throttling backfill, idling %ds", IDLE_SECONDS)
            time.sleep(IDLE_SECONDS)
            continue
        try:
            did_work = run_once()
        except (psycopg.Error, KeyError, ValueError) as exc:
            logger.error("cycle error: %s", exc)
            did_work = True  # back off briefly, then retry
        if not did_work:
            logger.info("history complete to target; idling %ds", IDLE_SECONDS)
            time.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    main()
