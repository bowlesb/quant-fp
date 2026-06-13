"""Materialize features into the Parquet store (the write path).

Computes every runnable group for a day + source and writes each to its source-tagged partition.
Same code serves the backfill write (``source=backfill``) and, fed live frames, the live write
(``source=stream``) — so what's stored is parity-true by construction.

Usage: python -m quantlib.features.materialize <root> <YYYY-MM-DD> <stream|backfill>
"""
from __future__ import annotations

import sys

from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_group
from quantlib.features.loaders import load_minute_agg


def materialize_minute(root: str, day: str, source: str, only_groups: list[str] | None = None) -> None:
    """Compute and write minute-aggregate groups for a day + source. ``only_groups`` scopes the work
    to specific groups — the REPAIR path: fix feature Y over period X by re-materializing only its
    group for those dates. Each (group, source, date) partition is independent, so a repair fans out
    in parallel across dates/groups with no contention and no global lock (atomic per partition)."""
    frames = {"minute_agg": load_minute_agg(day, source)}
    ctx = BatchContext(frames=frames)
    groups = [g for g in runnable(frames) if only_groups is None or g.name in only_groups]
    for group in groups:
        out = run_group(group, ctx, validate=False)
        store.write_group(root, group.name, group.version, source, day, out)
        print(f"wrote group={group.name} source={source} date={day}: {out.height} rows")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: python -m quantlib.features.materialize <root> <YYYY-MM-DD> <stream|backfill> [group,group]"
        )
    only = sys.argv[4].split(",") if len(sys.argv) > 4 else None
    materialize_minute(sys.argv[1], sys.argv[2], sys.argv[3], only)


if __name__ == "__main__":
    main()
