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


def materialize_minute(root: str, day: str, source: str) -> None:
    """Compute and write every minute-aggregate group for a day + source into the store."""
    frames = {"minute_agg": load_minute_agg(day, source)}
    ctx = BatchContext(frames=frames)
    for group in runnable(frames):
        out = run_group(group, ctx, validate=False)
        store.write_group(root, group.name, group.version, source, day, out)
        print(f"wrote group={group.name} source={source} date={day}: {out.height} rows")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.materialize <root> <YYYY-MM-DD> <stream|backfill>")
    materialize_minute(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
