"""On-disk contract for the shared `/store/raw/` dataset — partition layout + resumable manifest.

Pure storage helpers shared by the backfill orchestrator (``raw_backfill``) and the fast multiprocess
engine (``fast_backfill``). Kept in their own module so both engines depend on this leaf instead of each
other (no circular import).

Layout:  <store>/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet
Manifest: <store>/raw/_manifest_<tier>.d/part-*.parquet  (append-only parts; legacy single file unioned)
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "tier": pl.String,
    "symbol": pl.String,
    "date": pl.String,
    "rows": pl.Int64,
    "bytes": pl.Int64,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def partition_dir(store: str, tier: str, symbol: str, day: dt.date) -> str:
    return os.path.join(store, "raw", tier, f"symbol={symbol}", f"date={day.isoformat()}")


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


def write_manifest_part(store: str, tier: str, entries: list[dict], part_seq: int) -> None:
    """Persist a batch of manifest entries as ONE immutable append-only part file (atomic tmp+rename).
    Part name is (pid, seq)-unique so concurrent/overlapping processes never collide. Resume tolerates a
    crash mid-buffer: only the unflushed entries are lost and their partitions are simply re-fetched."""
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


def write_partition(store: str, tier: str, symbol: str, day: dt.date, frame: pl.DataFrame) -> int:
    """Write a symbol-day partition parquet; return on-disk byte size. Empty frames still write a
    zero-row file so the manifest marks the symbol-day DONE (no-data days are not re-fetched)."""
    out_dir = partition_dir(store, tier, symbol, day)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.parquet")
    tmp_path = os.path.join(out_dir, "data.parquet.tmp")
    frame.write_parquet(tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)
    return os.path.getsize(out_path)
