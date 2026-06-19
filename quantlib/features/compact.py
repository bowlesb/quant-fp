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
        python -m quantlib.features.compact <root> --settled    # every SETTLED stream day (< today)
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import polars as pl

from quantlib.features.base import KEY_COLUMNS
from quantlib.features.store import BATCH_ZSTD_LEVEL

COMPACTED_NAME = "data-compacted.parquet"
_DATE_DIR = re.compile(r"date=(\d{4}-\d{2}-\d{2})$")


def compact_partition(partition: Path) -> int:
    """Merge one partition's ``data*.parquet`` files into a single compacted file. Returns the number of
    per-minute files folded away (0 if there was nothing to compact)."""
    files = sorted(partition.glob("data*.parquet"))
    if not files or (len(files) == 1 and files[0].name == COMPACTED_NAME):
        return 0
    # missing_columns="insert" reconciles heterogeneous schemas across a partition's files: a
    # fragmented-restart session can leave a narrower per-minute file (a group emitting a subset of its
    # features in one window, the full set in another). The columns union to the superset, the absent
    # feature read as null — exactly as a globbing reader already unions these files today.
    merged = (
        pl.read_parquet(files, missing_columns="insert")
        .unique(subset=list(KEY_COLUMNS), keep="last")
        .sort(list(KEY_COLUMNS))
    )
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


def discover_stream_days(root: str | Path, source: str = "stream") -> list[str]:
    """Every distinct ``date=`` value that has a ``source`` partition on disk, ascending."""
    base = Path(root)
    days: set[str] = set()
    for partition in base.glob(f"group=*/v=*/source={source}/date=*"):
        match = _DATE_DIR.search(partition.name)
        if match:
            days.add(match.group(1))
    return sorted(days)


def compact_settled_days(
    root: str | Path, source: str = "stream", today: date | None = None
) -> dict[str, dict[str, int]]:
    """Compact every SETTLED ``source`` day under ``root`` — i.e. every on-disk day STRICTLY BEFORE
    ``today`` (the SYSTEM-LOCAL date, matching how fc keys its session partition via ``date +%F``). The
    live session only ever writes today's ``date=`` partition, so excluding today guarantees we never fold
    a partition that fc is still appending to. Idempotent (an already-compacted day folds 0 files).
    Returns {day: compact_day-result}; days with nothing to fold are omitted."""
    cutoff = today or date.today()
    result: dict[str, dict[str, int]] = {}
    for day in discover_stream_days(root, source):
        if date.fromisoformat(day) >= cutoff:
            continue  # today (or future) — fc may still be writing it; never touch
        folded = compact_day(root, day, source)
        if folded:
            result[day] = folded
    return result


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: python -m quantlib.features.compact <root> <YYYY-MM-DD> [stream|backfill]\n"
            "       python -m quantlib.features.compact <root> --settled [stream|backfill]"
        )
    root = sys.argv[1]
    if sys.argv[2] == "--settled":
        source = sys.argv[3] if len(sys.argv) > 3 else "stream"
        per_day = compact_settled_days(root, source)
        grand_total = sum(sum(folded.values()) for folded in per_day.values())
        print(
            f"compacted {len(per_day)} settled {source} day(s), folded {grand_total} per-minute "
            f"files -> {COMPACTED_NAME} (zstd-{BATCH_ZSTD_LEVEL})"
        )
        for day, folded in per_day.items():
            print(f"  {day}: {len(folded)} partitions, {sum(folded.values())} files folded")
        return
    day = sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "stream"
    folded = compact_day(root, day, source)
    total = sum(folded.values())
    print(f"compacted {len(folded)} partitions, folded {total} per-minute files -> {COMPACTED_NAME} (zstd-{BATCH_ZSTD_LEVEL})")
    for partition, count in folded.items():
        print(f"  {count:>6}  {partition}")


if __name__ == "__main__":
    main()
