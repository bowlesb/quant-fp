"""On-disk contract for the shared ``/store/news/`` dataset — a raw, append-only, manifest-tracked
news-article tape, mirroring the ``raw_store`` bars/trades/quotes layout but partitioned by DATE only
(news is a multi-symbol event, not a per-symbol-day OHLCV tape).

WHY DATE-PARTITIONED (not symbol=<S>/date=<D> like the raw bar tape): a single Alpaca article carries a
``symbols`` LIST (e.g. an AAPL/MSFT/SPY co-mention), so a per-symbol partition would duplicate every
multi-symbol article N times and force a join to de-dup. We instead store ONE row per article, keyed by
the article ``id``, with ``symbols`` as a list column, partitioned by the article's ``published_date``
(UTC date of ``available_at``). Hotness features (per-symbol counts/intensity over a trailing window)
EXPLODE the ``symbols`` list at read time — cheap, and the article is stored once.

Layout:  <store>/news/published_date=<YYYY-MM-DD>/data.parquet   (one parquet per UTC publish-date)
Manifest: <store>/news/_manifest.d/part-*.parquet                (append-only parts, unioned on load)

THE PARITY CONTRACT (the whole point — mirrors the edgar_filing_frequency design):

  * ``available_at`` is the look-ahead-safe, point-in-time field every future hotness feature keys off:
    a (symbol, minute) cell counts ONLY articles with ``available_at <= minute``.
  * ``available_at`` is FIXED AT FIRST SIGHT and NEVER rewritten. The store de-dups by article ``id``
    (an article is written exactly once, the first time it is seen — live OR backfill), so the
    ``available_at`` recorded for an id is immutable. A hotness feature gated purely on
    ``available_at <= minute`` therefore yields the IDENTICAL article set in live and in backfill =>
    backfill == live by construction, the same contract the EDGAR filing-frequency group nailed.
  * ``available_at`` SEMANTICS: the instant the article became visible. LIVE capture sets it to the
    websocket ARRIVAL time (when WE saw it) — never earlier than wall-clock, so it can never look ahead.
    BACKFILL sets it to Alpaca's ``created_at`` (the article's publish instant). The two can differ by the
    live feed delay; the explicit availability-lag the Modeller's hotness hunt applies (a fixed offset
    added on the READER side, frozen pre-data) makes the feature robust to that delay — see the hunt
    pre-registration. The STORE keeps the honest provenance (``available_at_source`` flags which one), and
    because a given id is written once, a day is parity-stable once it has been seen by either path.

This module is PURE STORAGE (no Alpaca client, no feature def, no fingerprint surface) — shared by the
live ``news_capture`` reader and the historical ``news_backfill`` seeder, kept a leaf so neither imports
the other.
"""
from __future__ import annotations

import datetime as dt
import glob
import logging
import os

import polars as pl

logger = logging.getLogger("news_store")

# One row per article. ``symbols`` is a LIST so a multi-symbol article is stored once; hotness features
# explode it at read time. ``available_at`` is the point-in-time gate; ``published_at`` is Alpaca's
# created_at (article publish instant) kept as honest metadata; ``available_at_source`` flags provenance.
NEWS_SCHEMA: dict[str, pl.DataType] = {
    "id": pl.Int64,
    "symbols": pl.List(pl.String),
    "available_at": pl.Datetime("us", "UTC"),
    "available_at_source": pl.String,
    "published_at": pl.Datetime("us", "UTC"),
    "updated_at": pl.Datetime("us", "UTC"),
    "headline": pl.String,
    "summary": pl.String,
    "source": pl.String,
    "author": pl.String,
    "url": pl.String,
    "ingested_at": pl.Datetime("us", "UTC"),
}

# "live" = available_at is the websocket arrival instant WE observed (never look-ahead).
# "backfill" = available_at is Alpaca created_at (the historical publish instant).
SRC_LIVE = "live_arrival"
SRC_BACKFILL = "alpaca_created"

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "published_date": pl.String,
    "articles": pl.Int64,
    "bytes": pl.Int64,
    "source": pl.String,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def partition_dir(store: str, published_date: dt.date) -> str:
    return os.path.join(store, "news", f"published_date={published_date.isoformat()}")


def partition_path(store: str, published_date: dt.date) -> str:
    return os.path.join(partition_dir(store, published_date), "data.parquet")


def manifest_dir(store: str) -> str:
    """Directory of append-only manifest PART files (one immutable part per flush)."""
    return os.path.join(store, "news", "_manifest.d")


def load_manifest(store: str) -> pl.DataFrame:
    """Union every append-only manifest part (empty schema'd frame when none exist)."""
    parts_dir = manifest_dir(store)
    frames: list[pl.DataFrame] = []
    if os.path.isdir(parts_dir):
        for name in sorted(os.listdir(parts_dir)):
            if name.endswith(".parquet"):
                frames.append(pl.read_parquet(os.path.join(parts_dir, name)))
    if not frames:
        return pl.DataFrame(schema=MANIFEST_SCHEMA)
    return pl.concat(frames, how="vertical") if len(frames) > 1 else frames[0]


def backfilled_dates(store: str) -> set[str]:
    """The set of ``published_date`` ISO strings a BACKFILL part has already recorded — the resume key.

    Live capture appends incrementally to today's partition all session, so its manifest parts are NOT a
    completion signal; only ``source == SRC_BACKFILL`` parts mark a historical date as fully seeded, so a
    backfill resume skips exactly the dates it already pulled."""
    manifest = load_manifest(store)
    if manifest.height == 0:
        return set()
    done = manifest.filter(pl.col("source") == SRC_BACKFILL)
    return set(done["published_date"].to_list())


def _read_partition(store: str, published_date: dt.date) -> pl.DataFrame:
    path = partition_path(store, published_date)
    if not os.path.exists(path):
        return pl.DataFrame(schema=NEWS_SCHEMA)
    return pl.read_parquet(path)


def _write_partition_atomic(store: str, published_date: dt.date, frame: pl.DataFrame) -> int:
    out_dir = partition_dir(store, published_date)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.parquet")
    tmp_path = os.path.join(out_dir, "data.parquet.tmp")
    frame.write_parquet(tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)
    return os.path.getsize(out_path)


def write_manifest_part(store: str, entries: list[dict], source: str) -> None:
    """Persist a batch of per-date manifest entries as ONE immutable append-only part (atomic tmp+rename)."""
    if not entries:
        return
    parts_dir = manifest_dir(store)
    os.makedirs(parts_dir, exist_ok=True)
    frame = pl.DataFrame(entries, schema=MANIFEST_SCHEMA)
    name = f"part-{os.getpid()}-{int(dt.datetime.now(dt.timezone.utc).timestamp() * 1e6):020d}.parquet"
    final_path = os.path.join(parts_dir, name)
    tmp_path = f"{final_path}.tmp"
    frame.write_parquet(tmp_path)
    os.replace(tmp_path, final_path)


def upsert_articles(store: str, articles: list[dict], source: str) -> int:
    """Merge ``articles`` into their per-published-date partitions, de-duping by ``id`` FIRST-SIGHT.

    Each dict must carry every ``NEWS_SCHEMA`` key. Articles are grouped by the UTC date of
    ``available_at`` (the partition key). For each touched date we read the existing partition, vertically
    concat the new rows, and keep ONE row per ``id`` — the EARLIEST ``ingested_at`` wins, so an id already
    on disk keeps its original ``available_at`` (fixed-at-first-sight => parity-stable). Returns the count
    of genuinely-new articles written. A manifest part is appended per touched date recording the new
    article count + on-disk bytes + provenance ``source``.
    """
    if not articles:
        return 0
    incoming = pl.DataFrame(articles, schema=NEWS_SCHEMA)
    incoming = incoming.with_columns(pl.col("available_at").dt.date().cast(pl.String).alias("_pdate"))
    total_new = 0
    manifest_entries: list[dict] = []
    for (pdate_iso,), group in incoming.group_by(["_pdate"], maintain_order=True):
        published_date = dt.date.fromisoformat(str(pdate_iso))
        existing = _read_partition(store, published_date)
        existing_ids = set(existing["id"].to_list()) if existing.height else set()
        merged = pl.concat([existing, group.drop("_pdate")], how="vertical")
        # First-sight wins: sort by ingested_at ascending, keep the first row per id.
        merged = (
            merged.sort("ingested_at", descending=False)
            .unique(subset=["id"], keep="first", maintain_order=True)
            .sort("available_at", descending=False)
        )
        new_count = merged.height - existing.height
        if new_count <= 0 and existing.height == merged.height:
            # Nothing new on disk for this date (all ids already present) — skip the rewrite.
            continue
        size = _write_partition_atomic(store, published_date, merged)
        new_in_group = len([i for i in group["id"].to_list() if i not in existing_ids])
        total_new += new_in_group
        manifest_entries.append(
            {
                "published_date": published_date.isoformat(),
                "articles": merged.height,
                "bytes": size,
                "source": source,
                "fetched_at": dt.datetime.now(dt.timezone.utc),
            }
        )
    write_manifest_part(store, manifest_entries, source)
    return total_new


_NEWS_READ_COLUMNS = [
    "id",
    "symbols",
    "available_at",
    "published_at",
    "headline",
    "summary",
    "source",
    "url",
]


def load_news(start_date_iso: str, end_date_iso: str, store: str = "/store") -> pl.DataFrame:
    """Load every article whose ``published_date`` partition falls in ``[start, end]`` (inclusive ISO
    dates) — the shared research/feature loader. One hive-glob scan; empty schema'd frame when no
    partitions match. The READER applies the ``available_at <= minute`` point-in-time gate (and the
    hunt's frozen availability-lag offset); this loader just returns the raw article tape for the window.
    """
    pattern = os.path.join(store, "news", "published_date=*", "data.parquet")
    paths = sorted(glob.glob(pattern))
    keep = []
    for path in paths:
        segment = [p for p in path.split(os.sep) if p.startswith("published_date=")]
        if not segment:
            continue
        date_iso = segment[0][len("published_date=") :]
        if start_date_iso <= date_iso <= end_date_iso:
            keep.append(path)
    if not keep:
        return pl.DataFrame(schema={key: NEWS_SCHEMA[key] for key in _NEWS_READ_COLUMNS})
    return pl.read_parquet(keep).select(_NEWS_READ_COLUMNS).sort("available_at")


def free_bytes(store: str) -> int:
    stats = os.statvfs(store)
    return stats.f_bavail * stats.f_frsize
