"""Bounded historical SEED for the ``/store/news/`` tape (Alpaca v1beta1/news).

Pulls recent news history for a liquid-core symbol list into the append-only, manifest-tracked
``news_store``, so the Modeller's hotness hunt has data to run against. Deliberately MEMORY-SAFE and
SMALL by default (single process, day-chunked, resumable) — this is the pilot/seed path, NOT a giant
universe-wide pull. Name the container ``quant-backfill*`` so the live_monitor memguard protects it.

Resumable: ``news_store.backfilled_dates`` records which UTC dates a BACKFILL part already seeded; a
re-run skips them. De-dup by article ``id`` (first-sight wins) makes a re-fetch idempotent regardless.

Run inside fp-dev (mirrors the raw backfill)::

    docker run -d --name quant-backfill-news --env-file .env \\
        -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.data.news_backfill --store /store --days 30 --top 100 --processes 1

Symbols: ``--symbols A,B,C`` (explicit) OR ``--top N`` (the N most-liquid universe names for the latest
seeded universe day, via the universe loader). ``--processes`` is accepted for parity with the raw
backfill CLI but the seed is single-process by design (Alpaca news pages are cheap; the memguard is the
real constraint) and values >1 are rejected so a careless invocation can't fan out.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os

from quantlib.data.news_fetchers import build_news_client, fetch_news_window
from quantlib.data.news_store import (
    SRC_BACKFILL,
    backfilled_dates,
    free_bytes,
    upsert_articles,
    write_manifest_part,
)
from quantlib.features.loaders import load_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("news_backfill")

DEFAULT_STORE = "/store"
MIN_FREE_BYTES = 5 * 1024**3  # refuse to write below 5 GiB free (NVMe headroom for live capture).
NEWS_SYMBOLS_PER_REQUEST = 50  # cap the symbols-per-Alpaca-call so the comma list stays bounded.


def latest_universe_day(store: str) -> str:
    """The most recent ``universe_membership`` day available (the seed's liquid-core source)."""
    today = dt.date.today()
    for back in range(0, 14):
        day = (today - dt.timedelta(days=back)).isoformat()
        frame = load_universe(day)
        if frame.height > 0:
            return day
    raise RuntimeError("no seeded universe_membership day found in the last 14 days")


def top_liquid_symbols(top_n: int, store: str) -> list[str]:
    """The ``top_n`` most-liquid universe symbols for the latest seeded day, ranked by ADV dollar.

    ``universe_membership`` is the in-universe screen; it may not carry adv_dollar populated (the seed
    leaves it NULL — see SYSTEM_LOG 07:35Z), so we fall back to the raw in-universe order when the
    liquidity column is missing, taking the first ``top_n``. The seed only needs a sensible LIQUID CORE,
    not a perfect ranking.
    """
    day = latest_universe_day(store)
    frame = load_universe(day)
    if "adv_dollar" in frame.columns and frame["adv_dollar"].null_count() < frame.height:
        frame = frame.sort("adv_dollar", descending=True, nulls_last=True)
    symbols = frame["symbol"].to_list()
    return symbols[:top_n]


def resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return top_liquid_symbols(args.top, args.store)


def day_windows(days: int, today: dt.date) -> list[dt.date]:
    """The list of UTC calendar dates to seed: the trailing ``days`` ending yesterday (today's tape is
    still settling and is owned by the live capture path)."""
    return [today - dt.timedelta(days=offset) for offset in range(1, days + 1)]


def seed_day(client, symbols: list[str], day: dt.date, store: str) -> int:  # type: ignore[no-untyped-def]
    """Fetch + persist all news for ``symbols`` on UTC calendar ``day``. Returns new-article count.

    Symbols are chunked into ``NEWS_SYMBOLS_PER_REQUEST`` groups so the comma-joined request stays
    bounded; each chunk's articles are upserted (de-dup by id across chunks is the store's job).
    """
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
    new_total = 0
    for offset in range(0, len(symbols), NEWS_SYMBOLS_PER_REQUEST):
        chunk = symbols[offset : offset + NEWS_SYMBOLS_PER_REQUEST]
        rows = fetch_news_window(client, chunk, start, end)
        new_total += upsert_articles(store, rows, source=SRC_BACKFILL)
    if new_total == 0:
        # Record an EMPTY backfill manifest entry so a no-news date is marked done (not re-fetched).
        write_manifest_part(
            store,
            [
                {
                    "published_date": day.isoformat(),
                    "articles": 0,
                    "bytes": 0,
                    "source": SRC_BACKFILL,
                    "fetched_at": dt.datetime.now(dt.timezone.utc),
                }
            ],
            SRC_BACKFILL,
        )
    return new_total


def run_backfill(args: argparse.Namespace) -> None:
    symbols = resolve_symbols(args)
    client = build_news_client(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    done = backfilled_dates(args.store)
    days = day_windows(args.days, dt.date.today())
    logger.info(
        "news seed: %d symbols, %d trailing days, store=%s, already-seeded=%d days",
        len(symbols),
        len(days),
        args.store,
        len(done),
    )
    seeded = 0
    for day in days:
        if day.isoformat() in done:
            continue
        if free_bytes(args.store) < MIN_FREE_BYTES:
            logger.error("store free space below %d bytes — STOPPING seed", MIN_FREE_BYTES)
            break
        new_count = seed_day(client, symbols, day, args.store)
        seeded += 1
        logger.info("seeded %s: %d new articles", day.isoformat(), new_count)
    logger.info("news seed complete: %d dates processed this run", seeded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded historical seed of the /store/news tape.")
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--days", type=int, default=30, help="trailing UTC days to seed (ending yesterday)")
    parser.add_argument(
        "--top", type=int, default=100, help="top-N liquid universe symbols (if --symbols unset)"
    )
    parser.add_argument(
        "--symbols", default=None, help="comma list => explicit symbol set (overrides --top)"
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=1,
        help="single-process by design; values >1 are rejected (the memguard is the real constraint)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.processes != 1:
        raise SystemExit("news_backfill is single-process by design (--processes must be 1)")
    run_backfill(args)


if __name__ == "__main__":
    main()
