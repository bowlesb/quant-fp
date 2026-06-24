"""News + EDGAR dashboard tab data — the LIVE STREAMING rate and the STORE COMPOSITION.

Read-side ONLY (a DB SELECT + a bounded polars scan of the news manifest / newest partitions); it never
writes anything and touches NEITHER the live ingesters (quant-edgar-1, news-capture) NOR the feature
pipeline / its fingerprint. It backs the dashboard's third top-level tab (alongside the coverage grid and
the latency view).

TWO SURFACES, two cost profiles:

  * STREAM (``stream_snapshot``) — CHEAP, recomputed on every request (no cache). The current articles/min +
    filings/min rate, a short recent-minute timeline, and the ACTIVE/WARN/STALE freshness status. The
    filings side is a tiny indexed recent-window query (hypertable chunk exclusion on ``available_at``,
    ~few ms); the news side reads only the newest partition. Freshness reuses ``data_freshness``'s
    business-hours-aware grading verbatim, so the tab and the cron alert agree on what "stale" means.

  * COMPOSITION (``composition_snapshot``) — heavier (full-table aggregates over ~3M filings: per-form-type
    breakdown + span), so it is served from a short-TTL in-process cache. Composition changes slowly (it is
    a store-shape snapshot, not a live rate), so a few-minute stale read is fine, and the heavy scan stays
    off the hot request path. The news side (totals + span + top symbols) is a cheap lazy column scan of
    the manifest / partitions.

The FEATURE-STATUS block in the composition payload reports what we actually compute off these tapes today:
``edgar_filing_frequency`` and ``news_sentiment`` are LIVE (parity-passing); ``news_hotness`` is COMING (not
yet registered). The news-sentiment summary slot in the payload is left for that summary to fill without a
contract change.
"""

from __future__ import annotations

import glob
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import polars as pl
import psycopg
from quantlib.features.validation_db import DB_KWARGS
from quantlib.ops.data_freshness import (
    NEWS_GLOB,
    SRC_LIVE,
    grade_age,
    in_business_hours,
    recent_news_partition_paths,
)

# The rate window for the live tab: count arrivals in the trailing N minutes and a per-minute timeline of the
# trailing M minutes. Both are small bounded reads.
RATE_WINDOW_MIN = 60
TIMELINE_WINDOW_MIN = 120

# Composition is a slowly-changing store snapshot; cache the heavy full-table aggregates this long so the ~3M
# row scans never land repeatedly on the request path. The live rate is on the separate uncached stream route.
COMPOSITION_TTL_SECONDS = 300

# How many top symbols (by article mention count) the composition surfaces, and how many form types.
TOP_SYMBOLS = 15
TOP_FORM_TYPES = 15

# The filings hypertable is partitioned by ``available_at`` into ~1700 seven-day chunks spanning 1994..now. A
# single un-bounded aggregate (count/group-by) plans against EVERY chunk and so takes a lock per chunk+index in
# one transaction — far past ``max_locks_per_transaction`` (the shared lock table overflows: "out of shared
# memory"). The composition still describes the WHOLE store, so we walk the full span in time WINDOWS, each its
# own autocommit query: ``available_at`` bounds let the planner exclude every out-of-window chunk, so each query
# locks only the handful of chunks it touches, and Python merges the per-window partials into the whole-store
# totals. 180 days ≈ 26 chunks/window — comfortably under the lock budget with margin for concurrent ingestion.
FILINGS_WINDOW_DAYS = 180

# Freshness thresholds mirror data_freshness (so the tab and the cron alert grade identically). Re-declared
# here as the tab's own knobs rather than imported, since data_freshness reads them from the env at import.
EDGAR_WARN_MIN = 45.0
EDGAR_FAIL_MIN = 120.0
NEWS_WARN_MIN = 90.0
NEWS_FAIL_MIN = 240.0

# The feature groups computed live off these tapes today (parity-passing): edgar_filing_frequency and
# news_sentiment. news_hotness is not yet registered and is reported as COMING.
FEATURE_STATUS = {
    "edgar_filing_frequency": {
        "label": "edgar_filing_frequency",
        "source": "EDGAR filings",
        "status": "LIVE",
        "detail": "per-(symbol, form_type) event-clock; parity-passing, computed live off the filings tape.",
    },
    "news_hotness": {
        "label": "news_hotness",
        "source": "Alpaca news",
        "status": "COMING",
        "detail": "per-symbol windowed article intensity off /store/news; pre-registered hunt, not yet live.",
    },
    "news_sentiment": {
        "label": "news_sentiment",
        "source": "Alpaca news",
        "status": "LIVE",
        "detail": "per-symbol windowed sentiment/count off /store/news (9 features); "
        "scored at ingest on both the live and backfill paths, parity-passing.",
    },
}


@dataclass
class _Cached:
    value: dict[str, object]
    expires_at: float


_composition_cache: _Cached | None = None


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _db_connect() -> psycopg.Connection:
    # autocommit: every read here is single-statement and read-only, and the windowed composition walk RELIES on
    # each query committing as it returns so its chunk locks release before the next window — otherwise the
    # whole walk would accumulate every window's locks in one transaction, defeating the chunk-bounded fix.
    return psycopg.connect(**DB_KWARGS, connect_timeout=8, autocommit=True)


def _status_for(
    newest: datetime | None, warn: float, fail: float, reference: datetime, source: str
) -> dict[str, object]:
    """Grade one source's freshness using data_freshness's business-hours-aware grader, flattened for the UI.

    Returns the status string + age + newest instant + whether the reference moment is in SEC business hours
    (so the UI can explain an INACTIVE as an expected weekend/overnight lull rather than a stall).
    """
    result = grade_age(source, newest, reference, warn, fail)
    return {
        "status": result.status.value,
        "age_minutes": round(result.age_minutes, 1) if result.age_minutes is not None else None,
        "newest_iso": result.newest_iso,
        "in_business_hours": in_business_hours(reference),
        "detail": result.detail,
    }


def _edgar_stream(conn: psycopg.Connection, reference: datetime) -> dict[str, object]:
    """Live EDGAR rate: stream-filings/min over the trailing window, a per-minute timeline, and freshness.

    All queries are bounded to a recent window so the hypertable excludes old chunks (indexed on
    ``available_at``) — a few ms even against ~3M rows. ``source='stream'`` excludes backfill rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM filings "
            "WHERE source = 'stream' AND available_at >= %s - make_interval(mins => %s)",
            (reference, RATE_WINDOW_MIN),
        )
        window_count = cur.fetchone()[0]
        # The freshness instant is the TRUE newest stream filing (its discovered_at — our wall-clock receipt),
        # not the windowed max: outside the rate window the window is empty, but the feed may simply be in a
        # weekend/overnight lull with a perfectly fresh-for-the-session newest. This mirrors
        # data_freshness.newest_edgar_ingest exactly so the tab and the cron alert grade the same instant.
        cur.execute("SELECT max(discovered_at) FROM filings WHERE source = 'stream'")
        newest = cur.fetchone()[0]
        cur.execute(
            "SELECT date_trunc('minute', available_at) AS minute, count(*) FROM filings "
            "WHERE source = 'stream' AND available_at >= %s - make_interval(mins => %s) "
            "GROUP BY minute ORDER BY minute",
            (reference, TIMELINE_WINDOW_MIN),
        )
        timeline = [
            {"minute": row[0].astimezone(timezone.utc).isoformat(), "count": int(row[1])}
            for row in cur.fetchall()
        ]
    if newest is not None and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return {
        "per_min": round(int(window_count) / RATE_WINDOW_MIN, 3),
        "window_count": int(window_count),
        "window_minutes": RATE_WINDOW_MIN,
        "timeline": timeline,
        "freshness": _status_for(newest, EDGAR_WARN_MIN, EDGAR_FAIL_MIN, reference, "edgar"),
    }


def _news_stream(reference: datetime) -> dict[str, object]:
    """Live news rate: live-arrival articles/min over the trailing window, a per-minute timeline, freshness.

    Reads only the newest news partition (today's UTC date) — the rate window is far inside it — so the scan
    is a single small parquet, never the whole dataset. ``available_at_source == SRC_LIVE`` keeps backfilled
    rows (whose available_at is the historical publish instant) from masking a live-feed stall.
    """
    paths = recent_news_partition_paths(
        2
    )  # today + yesterday covers the trailing window across midnight UTC
    if not paths:
        return {
            "per_min": 0.0,
            "window_count": 0,
            "window_minutes": RATE_WINDOW_MIN,
            "timeline": [],
            "freshness": _status_for(None, NEWS_WARN_MIN, NEWS_FAIL_MIN, reference, "news"),
        }
    window_start = reference - timedelta(minutes=TIMELINE_WINDOW_MIN)
    rate_start = reference - timedelta(minutes=RATE_WINDOW_MIN)
    # extra_columns="ignore": newer partitions carry a re-scored ``sentiment`` column absent from older ones,
    # so a multi-file scan sees a heterogeneous schema; tolerate the extra column instead of erroring on it.
    recent = (
        pl.scan_parquet(paths, extra_columns="ignore")
        .filter(pl.col("available_at_source") == SRC_LIVE)
        .filter(pl.col("available_at") >= window_start)
        .select(pl.col("available_at"))
        .collect()
    )
    newest = recent["available_at"].max() if recent.height else None
    if isinstance(newest, datetime) and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    window_count = int(recent.filter(pl.col("available_at") >= rate_start).height)
    timeline_df = (
        recent.with_columns(pl.col("available_at").dt.truncate("1m").alias("minute"))
        .group_by("minute")
        .agg(pl.len().alias("count"))
        .sort("minute")
    )
    timeline = [
        {"minute": row["minute"].astimezone(timezone.utc).isoformat(), "count": int(row["count"])}
        for row in timeline_df.iter_rows(named=True)
    ]
    return {
        "per_min": round(window_count / RATE_WINDOW_MIN, 3),
        "window_count": window_count,
        "window_minutes": RATE_WINDOW_MIN,
        "timeline": timeline,
        "freshness": _status_for(newest, NEWS_WARN_MIN, NEWS_FAIL_MIN, reference, "news"),
    }


def stream_snapshot() -> dict[str, object]:
    """The LIVE STREAMING panel payload — current articles/min + filings/min, recent timelines, freshness.

    Cheap and uncached: a few-ms recent-window filings query + the newest news partition scan. ``error`` keys
    carry a partial payload if a side is briefly unreachable rather than failing the whole tab.
    """
    reference = _now()
    payload: dict[str, object] = {"generated_at": reference.isoformat()}
    try:
        with _db_connect() as conn:
            payload["edgar"] = _edgar_stream(conn, reference)
    except psycopg.Error as exc:
        payload["edgar"] = {"error": f"DB unreachable: {exc}"}
    payload["news"] = _news_stream(reference)
    return payload


def _news_composition() -> dict[str, object]:
    """Total articles + span + top symbols + #symbols, from a cheap lazy column scan of the news partitions.

    Polars reads only the columns each aggregate needs (count/date from the manifest-free partition glob, the
    ``symbols`` list for the top-N), so even a full-history scan is a sub-second column read, not a row load.
    """
    paths = sorted(glob.glob(NEWS_GLOB))
    if not paths:
        return {
            "total_articles": 0,
            "n_symbols": 0,
            "earliest_date": None,
            "latest_date": None,
            "top_symbols": [],
        }
    # extra_columns="ignore": tolerate the ``sentiment`` column present only in re-scored newer partitions
    # (heterogeneous schema across the full-history glob) rather than erroring on the extra column.
    lazy = pl.scan_parquet(paths, extra_columns="ignore")
    total_articles = int(lazy.select(pl.len()).collect().item())
    symbols = lazy.select("symbols").explode("symbols").drop_nulls("symbols")
    n_symbols = int(symbols.select(pl.col("symbols").n_unique()).collect().item())
    top = (
        symbols.group_by("symbols")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
        .head(TOP_SYMBOLS)
        .collect()
    )
    top_symbols = [
        {"symbol": row["symbols"], "count": int(row["count"])} for row in top.iter_rows(named=True)
    ]
    earliest = paths[0].split("published_date=")[1].split("/")[0]
    latest = paths[-1].split("published_date=")[1].split("/")[0]
    return {
        "total_articles": total_articles,
        "n_symbols": n_symbols,
        "earliest_date": earliest,
        "latest_date": latest,
        "top_symbols": top_symbols,
    }


def _filings_span(conn: psycopg.Connection) -> tuple[datetime | None, datetime | None]:
    """The earliest/latest ``available_at`` in the store, each from an indexed ``ORDER BY .. LIMIT 1``.

    An ordered single-row read walks chunks newest/oldest-first and stops at the first row, so it locks one
    chunk, not all ~1700 (unlike ``min()``/``max()`` aggregates which plan against every chunk).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT available_at FROM filings ORDER BY available_at ASC LIMIT 1")
        earliest_row = cur.fetchone()
        cur.execute("SELECT available_at FROM filings ORDER BY available_at DESC LIMIT 1")
        latest_row = cur.fetchone()
    earliest = earliest_row[0] if earliest_row else None
    latest = latest_row[0] if latest_row else None
    return earliest, latest


def _filings_composition(conn: psycopg.Connection) -> dict[str, object]:
    """Total filings + span + per-form-type breakdown + live-stream count, from the filings hypertable.

    Walks the full store span in ``FILINGS_WINDOW_DAYS`` windows (each an ``available_at``-bounded query so the
    planner excludes out-of-window chunks and locks only a handful), merging the per-window partials into the
    whole-store totals. This keeps the snapshot whole-store while never locking all ~1700 chunks in one
    transaction. ``conn`` is in autocommit so each window's locks release as soon as its query returns. Heavy
    (~3M rows total) but paid behind the composition TTL cache, never on the hot request path.
    """
    earliest, latest = _filings_span(conn)
    if earliest is None or latest is None:
        return {
            "total_filings": 0,
            "stream_filings": 0,
            "earliest_available_at": None,
            "latest_available_at": None,
            "form_types": [],
        }
    total = 0
    stream_count = 0
    form_counts: Counter[str] = Counter()
    window = timedelta(days=FILINGS_WINDOW_DAYS)
    window_start = earliest
    with conn.cursor() as cur:
        while window_start <= latest:
            window_end = window_start + window
            cur.execute(
                "SELECT form_type, count(*) AS n, "
                "count(*) FILTER (WHERE source = 'stream') AS stream_n "
                "FROM filings WHERE available_at >= %s AND available_at < %s "
                "GROUP BY form_type",
                (window_start, window_end),
            )
            for form_type, count, stream_n in cur.fetchall():
                total += int(count)
                stream_count += int(stream_n)
                form_counts[form_type] += int(count)
            window_start = window_end
    form_types = [
        {"form_type": form_type, "count": count}
        for form_type, count in form_counts.most_common(TOP_FORM_TYPES)
    ]
    return {
        "total_filings": total,
        "stream_filings": stream_count,
        "earliest_available_at": earliest.astimezone(timezone.utc).isoformat(),
        "latest_available_at": latest.astimezone(timezone.utc).isoformat(),
        "form_types": form_types,
    }


def _build_composition() -> dict[str, object]:
    payload: dict[str, object] = {
        "generated_at": _now().isoformat(),
        "news": _news_composition(),
        "features": list(FEATURE_STATUS.values()),
    }
    try:
        with _db_connect() as conn:
            payload["filings"] = _filings_composition(conn)
    except psycopg.Error as exc:
        payload["filings"] = {"error": f"DB unreachable: {exc}"}
    return payload


def composition_snapshot() -> dict[str, object]:
    """The STORE COMPOSITION panel payload — totals/spans/top-symbols/per-form-type + feature status.

    Served from a short-TTL in-process cache (the heavy ~3M-row filings aggregates change slowly); the first
    request after expiry rebuilds it. ``cached``/``cache_age_seconds`` let the UI show how fresh the snapshot is.
    """
    global _composition_cache
    now = time.monotonic()
    if _composition_cache is not None and now < _composition_cache.expires_at:
        out = dict(_composition_cache.value)
        out["cached"] = True
        out["cache_age_seconds"] = round(COMPOSITION_TTL_SECONDS - (_composition_cache.expires_at - now), 1)
        return out
    value = _build_composition()
    _composition_cache = _Cached(value=value, expires_at=now + COMPOSITION_TTL_SECONDS)
    out = dict(value)
    out["cached"] = False
    out["cache_age_seconds"] = 0.0
    return out
