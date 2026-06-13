"""DB loaders for the parity harness — all I/O lives here so the group code stays pure.

These read the minute-aggregate inputs the platform computes features from. ``source='stream'`` is
what the running system captured live; ``source='backfill'`` is the settled historical-API tape —
the two sides of the T+1 Settled-Day Parity Test (FEATURE_PLATFORM.md §3.5).
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl
import psycopg

TICK_SCHEMA = {"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

_MINUTE_AGG_SQL = """
SELECT b.symbol, b.ts AS minute, b.open, b.close, b.high, b.low, b.volume,
       t.n_trades, t.signed_volume,
       q.mean_spread_bps, q.quote_imbalance, q.mean_bid_size, q.mean_ask_size
FROM bars_1m b
LEFT JOIN trade_agg_1m t
  ON t.symbol = b.symbol AND t.ts = b.ts AND t.source = %(source)s
LEFT JOIN quote_agg_1m q
  ON q.symbol = b.symbol AND q.ts = b.ts AND q.source = %(source)s
WHERE b.ts::date = %(day)s AND b.source = %(source)s
"""

_TIERS_SQL = """
SELECT symbol, adv_dollar
FROM universe_membership
WHERE trade_date = %(day)s AND in_universe AND adv_dollar IS NOT NULL
"""

# Slowly-changing per-symbol reference: sector (FMP, may be NULL until the key is wired) + Alpaca
# tradability flags. Static, so it is IDENTICAL for the live and backfill sources — sector/flag
# features are parity-true by construction. Based on asset_metadata so EVERY tradable symbol gets a
# row even when its sector is unmapped (left join -> NULL sector -> bucketed as "unknown").
_REFERENCE_SQL = """
SELECT a.symbol, s.sector,
       a.shortable, a.easy_to_borrow, a.marginable, a.fractionable
FROM asset_metadata a
LEFT JOIN sector_map s ON s.symbol = a.symbol
"""


def _query(sql: str, params: dict[str, str]) -> pl.DataFrame:
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [col.name for col in cur.description]
        data = cur.fetchall()
    # infer_schema_length=None scans all rows — columns like n_trades lead with nulls (LEFT JOIN
    # misses) and would otherwise be inferred too narrow from the first rows.
    return pl.DataFrame(data, schema=columns, orient="row", infer_schema_length=None)


def load_minute_agg(day: str, source: str) -> pl.DataFrame:
    """Minute-aggregate inputs (symbol, minute, close, n_trades, signed_volume) for one day+source."""
    return _query(_MINUTE_AGG_SQL, {"day": day, "source": source})


_TRADES_LIVE_SQL = """
SELECT symbol, ts, price, size FROM trades_raw
WHERE ts >= %(start)s AND ts < %(end)s AND symbol = ANY(%(symbols)s)
"""

def load_trades_live(start: dt.datetime, end: dt.datetime, symbols: list[str]) -> pl.DataFrame:
    """Raw ticks the running system captured (trades_raw) — the LIVE side of Layer-C parity."""
    frame = _query(_TRADES_LIVE_SQL, {"start": start, "end": end, "symbols": symbols})
    return frame.cast(TICK_SCHEMA) if frame.height else pl.DataFrame(schema=TICK_SCHEMA)


REFERENCE_SCHEMA = {
    "symbol": pl.String,
    "sector": pl.String,
    "shortable": pl.Boolean,
    "easy_to_borrow": pl.Boolean,
    "marginable": pl.Boolean,
    "fractionable": pl.Boolean,
}


def load_reference() -> pl.DataFrame:
    """Per-symbol reference snapshot (sector + tradability flags) for the sector/asset-flag features.
    Static and source-independent, so feeding it to both sides of the parity test yields trivial
    100% agreement — the point is point-in-time correctness, not live-vs-backfill skew."""
    frame = _query(_REFERENCE_SQL, {})
    if frame.height == 0:
        return pl.DataFrame(schema=REFERENCE_SCHEMA)
    return frame.cast(REFERENCE_SCHEMA)


def load_tiers(day: str) -> pl.DataFrame:
    """Liquidity tiers (Tier-1 top 500 / Tier-2 501–2000 / Tier-3 rest) by ADV$ for the day."""
    frame = _query(_TIERS_SQL, {"day": day})
    if frame.height == 0:
        return pl.DataFrame({"symbol": [], "tier": []}, schema={"symbol": pl.String, "tier": pl.Int32})
    return (
        frame.sort("adv_dollar", descending=True)
        .with_row_index("rank")
        .with_columns(
            pl.when(pl.col("rank") < 500)
            .then(1)
            .when(pl.col("rank") < 2000)
            .then(2)
            .otherwise(3)
            .cast(pl.Int32)
            .alias("tier")
        )
        .select("symbol", "tier")
    )
