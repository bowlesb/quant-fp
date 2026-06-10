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
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest, GetCalendarRequest

from quantlib.universe import SymbolStats, select_universe

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
data_client = StockHistoricalDataClient(
    os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
)

UNIVERSE_LOOKBACK_DAYS = int(os.environ.get("UNIVERSE_LOOKBACK_DAYS", "20"))
UNIVERSE_MAX_SYMBOLS = int(os.environ.get("UNIVERSE_MAX_SYMBOLS", "1000"))
UNIVERSE_CHUNK = 400


def tradable_equities() -> list[str]:
    """Active, tradable US common equities on major exchanges (no OTC)."""
    assets = trading.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    keep_exchanges = {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS"}
    return sorted(
        asset.symbol
        for asset in assets
        if asset.tradable
        and str(getattr(asset.exchange, "value", asset.exchange)) in keep_exchanges
        and "/" not in asset.symbol
    )


def fetch_symbol_stats(symbols: list[str], start: date) -> list[SymbolStats]:
    """Daily-bar ADV($) and latest close per symbol over the lookback window."""
    stats: list[SymbolStats] = []
    for i in range(0, len(symbols), UNIVERSE_CHUNK):
        chunk = symbols[i : i + UNIVERSE_CHUNK]
        request = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start
        )
        barset = data_client.get_stock_bars(request)
        for symbol, bars in barset.data.items():
            if not bars:
                continue
            adv = sum(bar.close * bar.volume for bar in bars) / len(bars)
            stats.append(SymbolStats(symbol=symbol, price=bars[-1].close, adv_dollar=adv))
        logger.info("universe stats: %d/%d symbols processed", min(i + UNIVERSE_CHUNK, len(symbols)), len(symbols))
    return stats


def refresh_asset_metadata(conn: psycopg.Connection) -> int:
    """Upsert per-symbol Alpaca asset metadata (exchange + shortable/borrow flags)."""
    assets = trading.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    rows = [
        (
            asset.symbol, asset.name,
            str(getattr(asset.exchange, "value", asset.exchange)),
            asset.tradable, asset.marginable, asset.shortable,
            asset.easy_to_borrow, asset.fractionable,
        )
        for asset in assets
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO asset_metadata
                   (symbol, name, exchange, tradable, marginable, shortable,
                    easy_to_borrow, fractionable, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (symbol) DO UPDATE SET
                   name=EXCLUDED.name, exchange=EXCLUDED.exchange,
                   tradable=EXCLUDED.tradable, marginable=EXCLUDED.marginable,
                   shortable=EXCLUDED.shortable, easy_to_borrow=EXCLUDED.easy_to_borrow,
                   fractionable=EXCLUDED.fractionable, updated_at=now()""",
            rows,
        )
    logger.info("asset_metadata refreshed: %d symbols", len(rows))
    return len(rows)


def maybe_refresh_asset_metadata() -> None:
    """Refresh once per day (or if empty)."""
    today = datetime.now(timezone.utc).date()
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(updated_at)::date FROM asset_metadata")
            row = cur.fetchone()
            if row and row[0] == today:
                return
        refresh_asset_metadata(conn)


def build_universe(conn: psycopg.Connection, trade_date: date) -> int:
    symbols = tradable_equities()
    logger.info("universe: %d tradable equities to screen", len(symbols))
    stats = fetch_symbol_stats(symbols, trade_date - timedelta(days=UNIVERSE_LOOKBACK_DAYS * 2))
    chosen = select_universe(stats, max_symbols=UNIVERSE_MAX_SYMBOLS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM universe_membership WHERE trade_date = %s", (trade_date,))
        for stat in chosen:
            cur.execute(
                """
                INSERT INTO universe_membership
                    (trade_date, symbol, in_universe, adv_dollar, price)
                VALUES (%s,%s,TRUE,%s,%s)
                """,
                (trade_date, stat.symbol, stat.adv_dollar, stat.price),
            )
    logger.info("universe %s: selected %d symbols", trade_date, len(chosen))
    return len(chosen)


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
            maybe_build_universe()
            maybe_refresh_asset_metadata()
        except (psycopg.Error, ValueError, KeyError) as exc:
            logger.error("daily job error: %s", exc)
        time.sleep(LOOP_SECONDS)


def maybe_build_universe() -> None:
    """Build today's universe once per day (skip if already present)."""
    today = datetime.now(timezone.utc).date()
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM universe_membership WHERE trade_date = %s",
                (today,),
            )
            row = cur.fetchone()
            if row and row[0] > 0:
                return
        build_universe(conn, today)


if __name__ == "__main__":
    main()
