"""Live real-feed capture launcher — the production entrypoint that wires the DB universe to the real
Alpaca SIP capture path.

Reads the in-universe symbols for the session ``trade_date`` from ``universe_membership`` (the set seeded
by ``seed_universe``), replicates the market-context index ETFs, then hands them to
``real_capture.run_sharded_capture`` against the REAL store. The reader owns one SIP websocket, subscribes
the universe's 1-minute bars, and idles until pre-market — then the sharded workers compute bar-derived
features each minute and write ``source=stream`` partitions.

Run inside the fp-dev image, --env-file .env, quant_default network, a REAL store volume:
    python -m quantlib.features.live_capture <trade_date YYYY-MM-DD> [store_root]

``ALPACA_DATA_FEED=sip`` and an UNSET ``STREAM_URL_OVERRIDE`` select the real consolidated tape.
"""
from __future__ import annotations

import os
import sys

import psycopg

from quantlib.features.real_capture import run_sharded_capture
from quantlib.features.sharded_capture import INDEX_SYMBOLS

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

_UNIVERSE_SQL = """
SELECT symbol FROM universe_membership
WHERE trade_date = %(day)s AND in_universe
ORDER BY symbol
"""


def load_universe_symbols(trade_date: str) -> list[str]:
    """The in-universe symbols for ``trade_date``, plus the index ETFs (deduped) the market-context
    groups need replicated to every shard."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_UNIVERSE_SQL, {"day": trade_date})
        symbols = [row[0] for row in cur.fetchall()]
    if not symbols:
        raise SystemExit(f"no in-universe symbols for {trade_date} — run seed_universe first")
    combined = sorted(set(symbols) | set(INDEX_SYMBOLS))
    return combined


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m quantlib.features.live_capture <trade_date YYYY-MM-DD> [store_root]")
    trade_date = sys.argv[1]
    root = sys.argv[2] if len(sys.argv) > 2 else "/store"
    symbols = load_universe_symbols(trade_date)
    print(
        f"[live_capture] day={trade_date} store={root} symbols={len(symbols)} "
        f"feed={os.environ.get('ALPACA_DATA_FEED', 'sip')} "
        f"override={os.environ.get('STREAM_URL_OVERRIDE') or '(real feed)'}",
        flush=True,
    )
    run_sharded_capture(symbols, root=root, mode="real", day=trade_date)


if __name__ == "__main__":
    main()
