"""Read-only TICKER-REPRESENTATION analysis over the feature store.

A standalone, symbol-centric companion to the dashboard's per-GROUP coverage surfaces
(``services/dashboard/feature_grid.py`` / ``store_grid.py``). Those answer "for THIS group, which
symbols are covered?"; this answers the inverse, cross-group question the warehouse needs for backfill
triage: "for THIS symbol, how broadly and how deeply is it represented across the whole store, and is it
under-represented on the LIVE stream vs the settled backfill?".

It is pure analysis/reporting — it READS partition directory names and a bounded SAMPLE of the
``symbol`` columns; it NEVER writes the store, changes schema/format, or touches a fingerprint. Run it
inside a container that has polars + the store mounted read-only (the host has neither), e.g.::

    docker exec quant-dashboard-1 python /app/ops/analyze_ticker_representation.py --store /store

It emits, for the union of all symbols seen anywhere in the store:

  * BREADTH      — number of groups a symbol appears in, per source (stream / backfill).
  * UNDER-REP    — groups where the symbol is BACKFILL-present but STREAM-absent (the FP_TICK_SYMBOLS
                   live-capture gap, aggregated per symbol rather than per group).
  * DEPTH        — the earliest backfill date the symbol is observed on (history reach) and a span in
                   days, estimated from a bounded sample of dates per group (full multi-year per-date
                   symbol reads would be minutes of I/O; the sample brackets the true earliest).

and three ranked lists: most under-represented-LIVE symbols, the coverage-depth distribution, and the
SHALLOWEST-history symbols (backfill-priority candidates — broadly traded today yet thin on history).

Output is human-readable text by default, or ``--json`` for a machine-readable blob.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

# Per-minute stream partitions hold thousands of ``data-<shard>-<epoch>.parquet`` files; the symbol
# universe is ~constant across a day's files, so an evenly-spaced bounded sample of the ``symbol`` column
# recovers the partition's symbol SET at a fixed cost. Backfill partitions are a single settled file.
MAX_FILES_PER_PARTITION = 12

# Reading the ``symbol`` set on EVERY date of a multi-year backfill group is the expensive part, so the
# two outputs read DIFFERENT bounded slices:
#
#   * BREADTH / under-representation is assessed on the most-recent settled BACKFILL dates (the anchor
#     window), where every currently-traded name appears — this gives an ACCURATE present-day group count
#     and a faithful stream-vs-backfill gap, not a sample-undercounted one.
#   * DEPTH samples a bounded set of dates per group (earliest N + latest N + an evenly-spaced interior
#     sample); the earliest date a symbol is seen on is a tight UPPER bound on its true first appearance
#     (it can only be earlier than the earliest SAMPLED date it was present on).
DEPTH_EDGE_DATES = 6
DEPTH_INTERIOR_DATES = 8

# Recent settled-backfill dates used for the present-day breadth / under-representation read.
BREADTH_BACKFILL_DATES = 3

DEFAULT_STORE_ROOT = os.environ.get("STORE_ROOT", "/store")


@dataclass
class SymbolRepr:
    """Cross-group representation evidence for one symbol."""

    symbol: str
    stream_groups: set[str] = field(default_factory=set)
    backfill_groups: set[str] = field(default_factory=set)
    earliest_backfill_date: str | None = None
    latest_backfill_date: str | None = None

    @property
    def under_rep_groups(self) -> set[str]:
        """Groups where the symbol is settled in backfill but absent from the live stream."""
        return self.backfill_groups - self.stream_groups

    @property
    def backfill_span_days(self) -> int:
        if self.earliest_backfill_date is None or self.latest_backfill_date is None:
            return 0
        first = dt.date.fromisoformat(self.earliest_backfill_date)
        last = dt.date.fromisoformat(self.latest_backfill_date)
        return (last - first).days


def list_groups(root: str) -> list[str]:
    base = Path(root)
    return sorted(path.name.removeprefix("group=") for path in base.glob("group=*") if path.is_dir())


def group_version(root: str, group: str) -> str | None:
    versions = sorted((Path(root) / f"group={group}").glob("v=*"))
    if not versions:
        return None
    # A group has a single active version on disk; if several, the lexically-latest is the current one.
    return versions[-1].name.removeprefix("v=")


def partition_dates(root: str, group: str, version: str, source: str) -> list[str]:
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return []
    return sorted(path.name.removeprefix("date=") for path in base.glob("date=*") if path.is_dir())


def _sample_files(files: list[Path], cap: int) -> list[Path]:
    """Up to ``cap`` evenly-spaced files (always first + last) — bounds the per-minute-file symbol read."""
    if len(files) <= cap:
        return files
    step = (len(files) - 1) / (cap - 1)
    indices = sorted({round(i * step) for i in range(cap)})
    return [files[i] for i in indices]


def read_symbols(root: str, group: str, version: str, source: str, date_iso: str) -> set[str]:
    """Distinct ``symbol`` set in one partition, from a bounded evenly-spaced file sample."""
    partition = Path(root) / f"group={group}" / f"v={version}" / f"source={source}" / f"date={date_iso}"
    files = sorted(partition.glob("data*.parquet"))
    if not files:
        return set()
    symbols: set[str] = set()
    for path in _sample_files(files, MAX_FILES_PER_PARTITION):
        symbols.update(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())
    return symbols


def sample_depth_dates(dates: list[str]) -> list[str]:
    """A bounded set of dates for the depth pass: the earliest+latest edges plus an interior spread."""
    if len(dates) <= 2 * DEPTH_EDGE_DATES + DEPTH_INTERIOR_DATES:
        return dates
    edges = dates[:DEPTH_EDGE_DATES] + dates[-DEPTH_EDGE_DATES:]
    interior = dates[DEPTH_EDGE_DATES:-DEPTH_EDGE_DATES]
    step = (len(interior) - 1) / (DEPTH_INTERIOR_DATES - 1)
    sampled_interior = [interior[round(i * step)] for i in range(DEPTH_INTERIOR_DATES)]
    return sorted(set(edges + sampled_interior))


def build_symbol_representation(root: str, anchor_window_days: int) -> dict[str, SymbolRepr]:
    """Walk every group: per-symbol stream/backfill group membership + backfill history depth.

    STREAM membership is read over the recent ``anchor_window_days`` window (the stream only spans the
    last few captured days, so a wider read finds nothing extra and costs more). BACKFILL membership +
    depth are read over a bounded sample of each group's dates (see ``sample_depth_dates``)."""
    symbols: dict[str, SymbolRepr] = {}

    def get(symbol: str) -> SymbolRepr:
        if symbol not in symbols:
            symbols[symbol] = SymbolRepr(symbol=symbol)
        return symbols[symbol]

    groups = list_groups(root)
    anchor = latest_date(root, groups)

    for group in groups:
        version = group_version(root, group)
        if version is None:
            continue

        # BREADTH: stream membership over the recent capture window; backfill membership over the most
        # recent settled dates (present-day group count, not sample-undercounted).
        stream_dates = partition_dates(root, group, version, "stream")
        if anchor is not None:
            cutoff = (anchor - dt.timedelta(days=anchor_window_days)).isoformat()
            stream_dates = [date_iso for date_iso in stream_dates if date_iso >= cutoff]
        for date_iso in stream_dates:
            for symbol in read_symbols(root, group, version, "stream", date_iso):
                get(symbol).stream_groups.add(group)

        backfill_dates = partition_dates(root, group, version, "backfill")
        for date_iso in backfill_dates[-BREADTH_BACKFILL_DATES:]:
            for symbol in read_symbols(root, group, version, "backfill", date_iso):
                get(symbol).backfill_groups.add(group)

        # DEPTH: bounded date sample → earliest/latest backfill date each symbol is observed on.
        for date_iso in sample_depth_dates(backfill_dates):
            for symbol in read_symbols(root, group, version, "backfill", date_iso):
                record = get(symbol)
                if record.earliest_backfill_date is None or date_iso < record.earliest_backfill_date:
                    record.earliest_backfill_date = date_iso
                if record.latest_backfill_date is None or date_iso > record.latest_backfill_date:
                    record.latest_backfill_date = date_iso

    return symbols


def latest_date(root: str, groups: list[str]) -> dt.date | None:
    """The most recent date with any partition across all groups/sources — the stream-window anchor."""
    latest: str | None = None
    for group in groups:
        version = group_version(root, group)
        if version is None:
            continue
        for source in ("stream", "backfill"):
            dates = partition_dates(root, group, version, source)
            if dates and (latest is None or dates[-1] > latest):
                latest = dates[-1]
    return dt.date.fromisoformat(latest) if latest is not None else None


def depth_distribution(records: list[SymbolRepr]) -> list[dict[str, object]]:
    """Histogram of symbols by backfill history-depth band (days from earliest backfill date to anchor)."""
    bands: list[tuple[str, int, int | None]] = [
        ("<=7d", 0, 7),
        ("8-30d", 8, 30),
        ("31-90d", 31, 90),
        ("91-180d", 91, 180),
        ("181-365d", 181, 365),
        (">365d", 366, None),
    ]
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        span = record.backfill_span_days
        for label, low, high in bands:
            if span >= low and (high is None or span <= high):
                counts[label] += 1
                break
    return [{"band": label, "n_symbols": counts[label]} for label, _, _ in bands]


def build_report(root: str, anchor_window_days: int, top_n: int) -> dict[str, object]:
    symbols = build_symbol_representation(root, anchor_window_days)
    records = list(symbols.values())
    n_total = len(records)
    n_groups = len(list_groups(root))
    anchor = latest_date(root, list_groups(root))

    # Symbols present in a RECENT backfill date (the present-day settled universe) — the denominator
    # that matters for live under-representation; a symbol never backfilled cannot be "under-represented".
    backfilled = [record for record in records if record.backfill_groups]
    stream_only = [record for record in records if record.stream_groups and not record.backfill_groups]
    # Every symbol with any settled history (incl. delisted names absent from recent dates) — the depth
    # histogram counts these so deep-but-gone names are not dropped.
    has_history = [record for record in records if record.earliest_backfill_date is not None]

    under_rep_ranked = sorted(
        (record for record in backfilled if record.under_rep_groups),
        key=lambda record: (len(record.under_rep_groups), len(record.backfill_groups)),
        reverse=True,
    )

    # Backfill-priority candidates: symbols broadly represented in backfill TODAY (so they are live
    # universe-relevant) yet SHALLOW in history — the names a deeper backfill would most extend.
    shallow_ranked = sorted(
        (record for record in backfilled if record.backfill_span_days >= 0),
        key=lambda record: (record.backfill_span_days, -len(record.backfill_groups)),
    )

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "store_root": root,
        "anchor_date": anchor.isoformat() if anchor else None,
        "stream_window_days": anchor_window_days,
        "n_groups": n_groups,
        "n_symbols_total": n_total,
        "n_symbols_backfilled": len(backfilled),
        "n_symbols_stream_only": len(stream_only),
        "n_symbols_under_rep_live": sum(1 for record in backfilled if record.under_rep_groups),
        "n_symbols_with_history": len(has_history),
        "depth_distribution": depth_distribution(has_history),
        "most_under_rep_live": [
            {
                "symbol": record.symbol,
                "under_rep_groups": len(record.under_rep_groups),
                "backfill_groups": len(record.backfill_groups),
                "stream_groups": len(record.stream_groups),
            }
            for record in under_rep_ranked[:top_n]
        ],
        "shallowest_history": [
            {
                "symbol": record.symbol,
                "backfill_span_days": record.backfill_span_days,
                "earliest_backfill_date": record.earliest_backfill_date,
                "backfill_groups": len(record.backfill_groups),
            }
            for record in shallow_ranked[:top_n]
        ],
        "stream_only_symbols": sorted(record.symbol for record in stream_only)[:top_n],
    }


def render_text(report: dict[str, object]) -> str:
    lines: list[str] = []
    lines.append("TICKER-REPRESENTATION ANALYSIS (read-only feature-store scan)")
    lines.append(f"  generated_at        {report['generated_at']}")
    lines.append(f"  store_root          {report['store_root']}")
    lines.append(
        f"  anchor_date         {report['anchor_date']}  (stream window {report['stream_window_days']}d)"
    )
    lines.append(f"  groups scanned      {report['n_groups']}")
    lines.append("")
    lines.append("UNIVERSE")
    lines.append(f"  symbols total       {report['n_symbols_total']}")
    lines.append(f"  in backfill         {report['n_symbols_backfilled']}")
    lines.append(
        f"  stream-only         {report['n_symbols_stream_only']}  (live but never settled-backfilled)"
    )
    lines.append(
        f"  under-rep LIVE      {report['n_symbols_under_rep_live']}  (backfill-present, stream-absent in >=1 group)"
    )
    lines.append("")
    lines.append(
        f"BACKFILL HISTORY-DEPTH DISTRIBUTION  ({report['n_symbols_with_history']} symbols with settled history)"
    )
    for band in report["depth_distribution"]:  # type: ignore[union-attr]
        lines.append(f"  {band['band']:<10} {band['n_symbols']:>6}")
    lines.append("")
    lines.append("MOST UNDER-REPRESENTED ON THE LIVE STREAM (backfill-present, stream-absent)")
    lines.append(f"  {'symbol':<10}{'under_rep':>10}{'backfill_g':>12}{'stream_g':>10}")
    for row in report["most_under_rep_live"]:  # type: ignore[union-attr]
        lines.append(
            f"  {row['symbol']:<10}{row['under_rep_groups']:>10}{row['backfill_groups']:>12}{row['stream_groups']:>10}"
        )
    lines.append("")
    lines.append("SHALLOWEST BACKFILL HISTORY (backfill-priority candidates: live-relevant, thin history)")
    lines.append(f"  {'symbol':<10}{'span_days':>10}{'earliest':>14}{'backfill_g':>12}")
    for row in report["shallowest_history"]:  # type: ignore[union-attr]
        lines.append(
            f"  {row['symbol']:<10}{row['backfill_span_days']:>10}{str(row['earliest_backfill_date']):>14}{row['backfill_groups']:>12}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only ticker-representation analysis of the feature store."
    )
    parser.add_argument(
        "--store", default=DEFAULT_STORE_ROOT, help="store root (default $STORE_ROOT or /store)"
    )
    parser.add_argument(
        "--stream-window-days", type=int, default=10, help="recent window for stream membership"
    )
    parser.add_argument("--top", type=int, default=40, help="length of each ranked list")
    parser.add_argument("--json", action="store_true", help="emit the machine-readable JSON blob")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not Path(args.store).exists():
        print(f"store root not found: {args.store}", file=sys.stderr)
        return 2
    report = build_report(args.store, args.stream_window_days, args.top)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
