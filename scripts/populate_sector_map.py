"""Populate ``sector_map`` (symbol -> GICS-aligned sector / industry) for the universe.

Source is yfinance (Yahoo), a keyless source whose sector labels ARE the GICS-aligned
text labels ``sector_map``/the ``sector`` one-hot group expect ("Technology",
"Financial Services", "Consumer Cyclical", ...). The legacy project's encrypted FMP
key is invalid (401), and yfinance was that project's actual production source anyway;
its 11 sector labels map one-to-one onto the consumer's canonical buckets.

Yahoo rate-limits ``Ticker.info`` aggressively, so we go through a ``curl_cffi`` Chrome-
impersonation session (real-browser TLS fingerprint) which restores access, with a
per-thread session, modest concurrency, and session-refresh + backoff on rate limits.

ETFs / preferreds / warrants / units / pre-deal SPACs legitimately have no GICS sector;
Yahoo returns ``None`` and we store NULL, which ``load_reference``'s LEFT JOIN + the
``sector`` group bucket as "unknown" (never dropped).

Symbol convention: the DB uses Alpaca dot notation (BRK.B); Yahoo needs dash (BRK-B).
We translate '.'->'-' for the QUERY only and store the original Alpaca symbol, since
``load_reference`` joins ``sector_map`` onto ``asset_metadata.symbol`` (dot notation).

Idempotent: upsert by symbol. Slowly-changing reference data, NOT a feature-compute /
fingerprint change. Run periodically (sector is weekly-fresh at most).

Usage:
    python scripts/populate_sector_map.py            # full asset_metadata universe
    python scripts/populate_sector_map.py --limit 50 # smoke a small sample
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg
import yfinance as yf
from curl_cffi import requests as creq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("populate_sector_map")

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

MAX_WORKERS = 6
MAX_RETRIES = 2

_thread_local = threading.local()


def get_session() -> creq.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = creq.Session(impersonate="chrome")
        _thread_local.session = session
    return session


def target_symbols(limit: int | None) -> list[str]:
    """The join key for ``load_reference`` is ``asset_metadata.symbol`` — fetch sectors
    for exactly that set so every tradable name resolves (mapped or explicit NULL)."""
    sql = "SELECT symbol FROM asset_metadata WHERE tradable ORDER BY symbol"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [row[0] for row in cur.fetchall()]


def fetch_one(symbol: str) -> tuple[str, str | None, str | None]:
    yahoo_symbol = symbol.replace(".", "-")
    for attempt in range(MAX_RETRIES + 1):
        try:
            info = yf.Ticker(yahoo_symbol, session=get_session()).info
            # Yahoo returns "" (not None) for unclassifiable names; normalize to NULL so the
            # canonical "unknown" bucket is a single representation (NULL), never an empty string.
            sector = info.get("sector") or None
            industry = info.get("industry") or None
            return symbol, sector, industry
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort the run
            if "RateLimit" in type(exc).__name__ or "Too Many" in str(exc):
                _thread_local.session = creq.Session(impersonate="chrome")
                time.sleep(2 * (attempt + 1))
                continue
            logger.debug("sector fetch failed for %s: %s", symbol, exc)
            break
    return symbol, None, None


def upsert(rows: list[tuple[str, str | None, str | None]]) -> None:
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO sector_map (symbol, sector, industry, source, updated_at)
                   VALUES (%s, %s, %s, 'yfinance', now())
               ON CONFLICT (symbol) DO UPDATE SET
                   sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                   source=EXCLUDED.source, updated_at=now()""",
            rows,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="cap symbols (smoke test)")
    args = parser.parse_args()

    symbols = target_symbols(args.limit)
    logger.info("populating sector_map for %d symbols", len(symbols))

    rows: list[tuple[str, str | None, str | None]] = []
    mapped = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, symbol) for symbol in symbols]
        for done, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            rows.append(row)
            if row[1]:
                mapped += 1
            if done % 500 == 0:
                upsert(rows)
                rows.clear()
                rate = done / (time.time() - started)
                logger.info("  %d/%d done, %d mapped, %.1f/s", done, len(symbols), mapped, rate)
    if rows:
        upsert(rows)

    null_rate = 1 - mapped / max(len(symbols), 1)
    logger.info(
        "sector_map populated: %d symbols, %d mapped, null-sector rate %.1f%%",
        len(symbols),
        mapped,
        100 * null_rate,
    )


if __name__ == "__main__":
    main()
