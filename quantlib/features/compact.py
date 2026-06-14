"""Daily compaction — fold the many per-minute streaming files into one file per partition.

The live path appends one small file per (shard, minute) for O(1), crash-safe writes (``store.write_group``
``minute=`` path) — ~864 files/minute cluster-wide, fine intraday but not what a settled day should leave
behind. This runs T+1 (or right after the session): for each ``source=stream`` partition of the day, union
its ``data-*.parquet``, de-dup on (symbol, minute) keep-last (idempotent over re-delivered minutes),
rewrite as a single high-ratio ``data-compacted.parquet`` (zstd-19), then delete the per-minute files.

Atomic and re-runnable: the compacted file is placed via ``os.replace`` BEFORE the per-minute files are
removed, so a crash mid-compaction leaves a correct (if not-yet-tidy) union that a re-run finishes. The
narrowed storage dtypes are preserved (the files are already Float32 / UInt8 / small-int on disk).

Usage:  python -m quantlib.features.compact <root> <YYYY-MM-DD> [stream|backfill]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import polars as pl

from quantlib.features.base import KEY_COLUMNS
from quantlib.features.store import BATCH_ZSTD_LEVEL

COMPACTED_NAME = "data-compacted.parquet"


def compact_partition(partition: Path) -> int:
    """Merge one partition's ``data*.parquet`` files into a single compacted file. Returns the number of
    per-minute files folded away (0 if there was nothing to compact)."""
    files = sorted(partition.glob("data*.parquet"))
    if not files or (len(files) == 1 and files[0].name == COMPACTED_NAME):
        return 0
    merged = pl.read_parquet(files).unique(subset=list(KEY_COLUMNS), keep="last").sort(list(KEY_COLUMNS))
    tmp = partition / f".tmp-compact.{os.getpid()}"
    merged.write_parquet(tmp, compression="zstd", compression_level=BATCH_ZSTD_LEVEL)
    os.replace(tmp, partition / COMPACTED_NAME)  # authoritative compacted file in place first (crash-safe)
    removed = 0
    for path in files:
        if path.name != COMPACTED_NAME:
            path.unlink()  # the per-minute/shard files are now subsumed by the compacted file
            removed += 1
    return removed


def compact_day(root: str | Path, day: str, source: str = "stream") -> dict[str, int]:
    """Compact every ``source`` partition of ``day`` under ``root``. Returns {partition_path: files_folded}."""
    base = Path(root)
    result: dict[str, int] = {}
    for partition in sorted(base.glob(f"group=*/v=*/source={source}/date={day}")):
        folded = compact_partition(partition)
        if folded:
            result[str(partition)] = folded
    return result


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m quantlib.features.compact <root> <YYYY-MM-DD> [stream|backfill]")
    root, day = sys.argv[1], sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "stream"
    folded = compact_day(root, day, source)
    total = sum(folded.values())
    print(f"compacted {len(folded)} partitions, folded {total} per-minute files -> {COMPACTED_NAME} (zstd-{BATCH_ZSTD_LEVEL})")
    for partition, count in folded.items():
        print(f"  {count:>6}  {partition}")


if __name__ == "__main__":
    main()
