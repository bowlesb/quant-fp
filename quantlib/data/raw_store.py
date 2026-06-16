"""On-disk contract for the shared `/store/raw/` dataset — partition layout + resumable manifest.

Pure storage helpers shared by the backfill orchestrator (``raw_backfill``) and the fast multiprocess
engine (``fast_backfill``). Kept in their own module so both engines depend on this leaf instead of each
other (no circular import).

Layout:  <store>/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet
Manifest: <store>/raw/_manifest_<tier>.d/part-*.parquet  (append-only parts; legacy single file unioned)
"""

from __future__ import annotations

import datetime as dt
import glob
import logging
import os

import polars as pl

logger = logging.getLogger("raw_store")

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "tier": pl.String,
    "symbol": pl.String,
    "date": pl.String,
    "rows": pl.Int64,
    "bytes": pl.Int64,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def partition_dir(store: str, tier: str, symbol: str, day: dt.date) -> str:
    return os.path.join(
        store, "raw", tier, f"symbol={symbol}", f"date={day.isoformat()}"
    )


def manifest_path(store: str, tier: str) -> str:
    return os.path.join(store, "raw", f"_manifest_{tier}.parquet")


def manifest_dir(store: str, tier: str) -> str:
    """Directory of append-only manifest PART files. Each flush writes one immutable part — recording a
    partition is then O(part size), not O(total manifest). load_manifest unions all parts (+ any legacy
    single-file manifest from an earlier run), so resume sees every prior fetch."""
    return os.path.join(store, "raw", f"_manifest_{tier}.d")


def load_manifest(store: str, tier: str) -> pl.DataFrame:
    """Union the legacy single-file manifest (if present) with every append-only part file."""
    frames = []
    legacy = manifest_path(store, tier)
    if os.path.exists(legacy):
        frames.append(pl.read_parquet(legacy))
    parts_dir = manifest_dir(store, tier)
    if os.path.isdir(parts_dir):
        for name in sorted(os.listdir(parts_dir)):
            if name.endswith(".parquet"):
                frames.append(pl.read_parquet(os.path.join(parts_dir, name)))
    if not frames:
        return pl.DataFrame(schema=MANIFEST_SCHEMA)
    return pl.concat(frames, how="vertical") if len(frames) > 1 else frames[0]


def done_keys(manifest: pl.DataFrame) -> set[tuple[str, str]]:
    """Set of (symbol, date) already recorded in a tier manifest."""
    if manifest.height == 0:
        return set()
    return {
        (symbol, date)
        for symbol, date in zip(
            manifest["symbol"].to_list(), manifest["date"].to_list()
        )
    }


def write_manifest_part(
    store: str, tier: str, entries: list[dict], part_seq: int
) -> None:
    """Persist a batch of manifest entries as ONE immutable append-only part file (atomic tmp+rename).
    Part name is (pid, seq)-unique so concurrent/overlapping processes never collide. Resume tolerates a
    crash mid-buffer: only the unflushed entries are lost and their partitions are simply re-fetched.
    """
    if not entries:
        return
    parts_dir = manifest_dir(store, tier)
    os.makedirs(parts_dir, exist_ok=True)
    frame = pl.DataFrame(entries, schema=MANIFEST_SCHEMA)
    name = f"part-{os.getpid()}-{part_seq:08d}.parquet"
    final_path = os.path.join(parts_dir, name)
    tmp_path = f"{final_path}.tmp"
    frame.write_parquet(tmp_path)
    os.replace(tmp_path, final_path)


def free_bytes(store: str) -> int:
    stats = os.statvfs(store)
    return stats.f_bavail * stats.f_frsize


def write_partition(
    store: str, tier: str, symbol: str, day: dt.date, frame: pl.DataFrame
) -> int:
    """Write a symbol-day partition parquet; return on-disk byte size. Empty frames still write a
    zero-row file so the manifest marks the symbol-day DONE (no-data days are not re-fetched).
    """
    out_dir = partition_dir(store, tier, symbol, day)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.parquet")
    tmp_path = os.path.join(out_dir, "data.parquet.tmp")
    frame.write_parquet(tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)
    return os.path.getsize(out_path)


def _parse_partition_path(path: str) -> tuple[str, str] | None:
    """Extract (symbol, date_iso) from a partition data.parquet path of the form
    ``.../symbol=<S>/date=<YYYY-MM-DD>/data.parquet``. Returns None if the path doesn't match.
    """
    parts = path.split(os.sep)
    symbol = None
    date_iso = None
    for segment in parts:
        if segment.startswith("symbol="):
            symbol = segment[len("symbol=") :]
        elif segment.startswith("date="):
            date_iso = segment[len("date=") :]
    if symbol is None or date_iso is None:
        return None
    return symbol, date_iso


def reconcile_manifest_from_disk(store: str, tier: str) -> int:
    """Record any on-disk partition that is MISSING from the manifest, returning the count reconciled.

    The manifest buffer flushes only every MANIFEST_FLUSH_EVERY units, so an OOM/crash that kills a worker
    loses its unflushed entries even though the partitions were already written to disk (write_partition
    runs before record). On the next resume those orphaned symbol-days look "pending" and are redundantly
    re-fetched — wasteful (idempotent, but it can re-pull >100k complete units). This scans the partition
    tree and appends one manifest part for the orphans (rows/bytes read from the parquet on disk), so a
    subsequent resume SKIPS them. Idempotent: a second run finds nothing to reconcile.
    """
    manifest = load_manifest(store, tier)
    done = done_keys(manifest)
    pattern = os.path.join(store, "raw", tier, "symbol=*", "date=*", "data.parquet")
    orphans: list[dict] = []
    now = dt.datetime.now(dt.timezone.utc)
    for path in glob.iglob(pattern):
        key = _parse_partition_path(path)
        if key is None or key in done:
            continue
        symbol, date_iso = key
        size = os.path.getsize(path)
        rows = pl.scan_parquet(path).select(pl.len()).collect().item()
        orphans.append(
            {
                "tier": tier,
                "symbol": symbol,
                "date": date_iso,
                "rows": rows,
                "bytes": size,
                "fetched_at": now,
            }
        )
        done.add(key)
    if orphans:
        existing_parts = glob.glob(os.path.join(manifest_dir(store, tier), "*.parquet"))
        write_manifest_part(store, tier, orphans, len(existing_parts) + 1)
    logger.info(
        "tier=%s reconciled %d orphaned on-disk partitions into the manifest",
        tier,
        len(orphans),
    )
    return len(orphans)
