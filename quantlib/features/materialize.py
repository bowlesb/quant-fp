"""Materialize features into the Parquet store (the write path).

Computes every runnable group for a day + source and writes each to its source-tagged partition.
Same code serves the backfill write (``source=backfill``) and, fed live frames, the live write
(``source=stream``) — so what's stored is parity-true by construction.

Usage: python -m quantlib.features.materialize <root> <YYYY-MM-DD> <stream|backfill>
"""
from __future__ import annotations

import sys

from quantlib.features import store
from quantlib.features.backfill_bars import backfill_bars, backfill_daily, tradable_universe
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_group
from quantlib.features.loaders import load_minute_agg, load_reference


def _write_all(root: str, day: str, source: str, frames: dict) -> int:
    ctx = BatchContext(frames=frames)
    for group in runnable(frames):
        out = run_group(group, ctx, validate=False)
        store.write_group(root, group.name, group.version, source, day, out)
        print(f"wrote group={group.name} source={source} date={day}: {out.height} rows")
    return frames["minute_agg"]["symbol"].n_unique() if frames["minute_agg"].height else 0


def materialize_alpaca_bars(root: str, day: str, symbols: list[str]) -> int:
    """Backfill bars for ANY symbols directly from Alpaca and write the bar features. Also loads the
    DAILY history so the multi-day features compute + broadcast (the full minute + daily horizon
    set). Returns the symbol count materialized."""
    frames = {
        "minute_agg": backfill_bars(day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
    }
    return _write_all(root, day, "backfill", frames)


def materialize_minute(root: str, day: str, source: str, only_groups: list[str] | None = None) -> None:
    """Compute and write minute-aggregate groups for a day + source. ``only_groups`` scopes the work
    to specific groups — the REPAIR path: fix feature Y over period X by re-materializing only its
    group for those dates. Each (group, source, date) partition is independent, so a repair fans out
    in parallel across dates/groups with no contention and no global lock (atomic per partition)."""
    frames = {"minute_agg": load_minute_agg(day, source), "reference": load_reference()}
    ctx = BatchContext(frames=frames)
    groups = [g for g in runnable(frames) if only_groups is None or g.name in only_groups]
    for group in groups:
        out = run_group(group, ctx, validate=False)
        store.write_group(root, group.name, group.version, source, day, out)
        print(f"wrote group={group.name} source={source} date={day}: {out.height} rows")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "alpaca":
        # alpaca <root> <day> <n_symbols>: backfill N tickers straight from Alpaca into the store
        root, day, n = args[1], args[2], int(args[3])
        symbols = tradable_universe(limit=n)
        count = materialize_alpaca_bars(root, day, symbols)
        print(f"materialized {count} symbols from Alpaca for {day} (requested {len(symbols)})")
        return
    if len(args) < 3:
        raise SystemExit(
            "usage: materialize <root> <day> <stream|backfill> [group,..]  |  materialize alpaca <root> <day> <n>"
        )
    only = args[3].split(",") if len(args) > 3 else None
    materialize_minute(args[0], args[1], args[2], only)


if __name__ == "__main__":
    main()
