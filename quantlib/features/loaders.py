"""DB loaders for the parity harness — all I/O lives here so the group code stays pure.

These read the minute-aggregate inputs the platform computes features from. ``source='stream'`` is
what the running system captured live; ``source='backfill'`` is the settled historical-API tape —
the two sides of the T+1 Settled-Day Parity Test (FEATURE_PLATFORM.md §3.5).
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl
import psycopg

from quantlib.data.news_store import load_news

_BEHAVIORAL_CLUSTERS_PATH = (
    Path(__file__).parent / "data" / "behavioral_clusters_v1.parquet"
)

TICK_SCHEMA = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "price": pl.Float64,
    "size": pl.Float64,
}

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

_UNIVERSE_SQL = """
SELECT symbol
FROM universe_membership
WHERE trade_date = %(day)s AND in_universe
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

# A SESSION snapshot of the EDGAR filings event store for one day, with a LOOKBACK so the trailing
# count windows (deepest 90d) AND the 365d burst baseline are correct at the session start. available_at
# is the point-in-time field (fixed at first sight); the per-minute available_at<=minute gate inside the
# group makes it point-in-time, so loading the whole [day - lookback, day_end) window up front is correct.
_FILINGS_SQL = """
SELECT symbol, form_type, available_at
FROM filings
WHERE symbol IS NOT NULL
  AND available_at >= %(start)s
  AND available_at < %(day_end)s
"""
# The burst baseline reads the trailing 365 days — the deepest window the group touches, so the snapshot
# must reach back at least this far (a few slack days for the calendar-day window edge).
FILINGS_LOOKBACK_DAYS = 370


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


def load_trades_live(
    start: dt.datetime, end: dt.datetime, symbols: list[str]
) -> pl.DataFrame:
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
    "cluster_id": pl.Int32,
}


def load_behavioral_clusters() -> pl.DataFrame:
    """The FROZEN symbol -> behavioral-cluster lookup (from the #76 SVD co-movement embedding, 11
    clusters / 2,722 symbols, cohesion held-out 0.092 vs 0.0003 random). A STATIC committed file
    refreshed nightly from settled daily bars — identical in stream and backfill, so any feature that
    joins on it is parity-true by construction (no intraday state). Symbols absent from the map get a
    NULL cluster_id (left unmapped, not bucketed)."""
    if not _BEHAVIORAL_CLUSTERS_PATH.exists():
        return pl.DataFrame(schema={"symbol": pl.String, "cluster_id": pl.Int32})
    return pl.read_parquet(_BEHAVIORAL_CLUSTERS_PATH).select(
        pl.col("symbol").cast(pl.String), pl.col("cluster_id").cast(pl.Int32)
    )


def load_reference() -> pl.DataFrame:
    """Per-symbol reference snapshot (sector + tradability flags) for the sector/asset-flag features.
    Static and source-independent, so feeding it to both sides of the parity test yields trivial
    100% agreement — the point is point-in-time correctness, not live-vs-backfill skew.
    """
    frame = _query(_REFERENCE_SQL, {})
    clusters = load_behavioral_clusters()
    if frame.height == 0:
        return pl.DataFrame(schema=REFERENCE_SCHEMA)
    return frame.join(clusters, on="symbol", how="left").cast(REFERENCE_SCHEMA)


UNIVERSE_SCHEMA = {"symbol": pl.String}


def load_universe(day: str) -> pl.DataFrame:
    """The day's FIXED in-universe symbol set (a one-column ``symbol`` frame) — the SAME membership the
    parity harness pins ranks to (``parity.parity_test``). cross_sectional_rank ranks ONLY within this set
    so live and backfill rank the identical symbols regardless of which names happened to print a given
    minute; without it each minute ranks over "whoever printed", a parity hazard. Source-independent
    (universe_membership is settled before the session), so feeding it to live and backfill is parity-true.
    """
    frame = _query(_UNIVERSE_SQL, {"day": day})
    if frame.height == 0:
        return pl.DataFrame(schema=UNIVERSE_SCHEMA)
    return frame.cast(UNIVERSE_SCHEMA)


FILINGS_SCHEMA = {
    "symbol": pl.String,
    "form_type": pl.String,
    "available_at": pl.Datetime("us", "UTC"),
}


def load_filings(day: str) -> pl.DataFrame:
    """Session snapshot of the EDGAR filings event store for ``day``: every filing whose ``available_at``
    falls in ``[day_start - FILINGS_LOOKBACK_DAYS, day_end)``. Loaded ONCE per session like ``daily`` /
    ``reference``; the per-minute ``available_at <= minute`` gate inside the edgar group makes it
    point-in-time. ``available_at`` is fixed at first sight, so feeding this identical frame to the live
    and backfill sides is parity-true by construction (the same compute-time-join contract as the
    reference snapshot, extended with the point-in-time gate). The lookback must cover the deepest window
    the group reads (the 365-day burst baseline) so the trailing counts and minutes-since-last are correct
    from the session's first minute, not just from the day's own filings."""
    day_date = dt.date.fromisoformat(day)
    day_end = day_date + dt.timedelta(days=1)
    start = day_date - dt.timedelta(days=FILINGS_LOOKBACK_DAYS)
    frame = _query(
        _FILINGS_SQL,
        {"start": start.isoformat(), "day_end": day_end.isoformat()},
    )
    if frame.height == 0:
        return pl.DataFrame(schema=FILINGS_SCHEMA)
    return frame.cast(FILINGS_SCHEMA)


NEWS_SCHEMA_FEATURE = {
    "symbol": pl.String,
    "available_at": pl.Datetime("us", "UTC"),
    "sentiment": pl.Float64,
}
# The news_sentiment group's deepest trailing window is 7 calendar days; load a few slack days beyond it so
# the trailing sums/means are correct from the session's first minute, not just from the day's own articles.
NEWS_LOOKBACK_DAYS = 9
NEWS_STORE_ROOT = os.environ.get("FP_NEWS_STORE", "/store")


def load_news_features(day: str) -> pl.DataFrame:
    """Session snapshot of the ``/store/news`` tape for ``day``, EXPLODED to one row per (symbol, article):
    every article whose ``available_at`` date falls in ``[day - NEWS_LOOKBACK_DAYS, day]``, with its baseline
    ``sentiment``. Loaded ONCE per session like ``filings`` / ``daily`` / ``reference``; the per-minute
    ``available_at <= minute`` gate inside the news_sentiment group makes it point-in-time, so the same
    snapshot fed to the live and backfill sides is parity-true by construction (``available_at`` AND
    ``sentiment`` are both fixed at first sight — the score is a deterministic function of the article's own
    text, identical live vs backfill). A multi-symbol article is counted once per tagged symbol (the explode),
    matching how a hotness/sentiment feature attributes an article to each name it mentions."""
    day_date = dt.date.fromisoformat(day)
    start = day_date - dt.timedelta(days=NEWS_LOOKBACK_DAYS)
    articles = load_news(start.isoformat(), day_date.isoformat(), store=NEWS_STORE_ROOT)
    if articles.height == 0:
        return pl.DataFrame(schema=NEWS_SCHEMA_FEATURE)
    exploded = (
        articles.select(["symbols", "available_at", "sentiment"])
        .explode("symbols")
        .rename({"symbols": "symbol"})
        .filter(pl.col("symbol").is_not_null())
        .select(["symbol", "available_at", "sentiment"])
    )
    return exploded.cast(NEWS_SCHEMA_FEATURE)


def load_tiers(day: str) -> pl.DataFrame:
    """Liquidity tiers (Tier-1 top 500 / Tier-2 501–2000 / Tier-3 rest) by ADV$ for the day."""
    frame = _query(_TIERS_SQL, {"day": day})
    if frame.height == 0:
        return pl.DataFrame(
            {"symbol": [], "tier": []}, schema={"symbol": pl.String, "tier": pl.Int32}
        )
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
