"""Read-only UNDER-REPRESENTED-TICKER flag: the FP_TICK_SYMBOLS streaming gap, ranked across the universe.

``ops/ticker_coverage.py`` (#463) surfaces the per-ticker LIVE GAP — groups a symbol is settled in BACKFILL
but absent from the live STREAM — for one name at a time. This rolls that same gap up across the WHOLE
universe into a fleet-level triage list: which symbols is the feature-computer NOT streaming despite having
settled backfill coverage, and which groups leak the most symbols.

It is the focused live-gap view the older aggregate ``ops/analyze_ticker_representation.py`` buries among
breadth/depth: here the single signal is ``under_rep_score`` = the number of groups a symbol is
backfill-present-but-stream-absent in. A high score = a broadly-backfilled name the live feed is missing.

PURE READ, and CPU-LIGHT BY DESIGN. The live gap needs only (a) who is SETTLED right now — a few RECENT
backfill dates — and (b) who is LIVE — the recent STREAM window. It does NOT need multi-year history, so the
walk is bounded to ``--backfill-dates`` recent settled dates + a ``--stream-days`` window per group, each read
via the bounded file-sample reused from ``ticker_coverage`` (<=12 files per partition). It never writes the
store, changes schema/format, or touches a fingerprint, and pulls NO feature engine (the reader import is
engine-free; there is no trust/DB dependency here at all).

The universe walk is factored over the ``StoreReader`` protocol (shared with ``ticker_coverage``), so the
aggregation + ranking logic is unit-tested with an in-memory fake — no /store mount, no DB. Only the concrete
``PartitionStoreReader`` touches disk; run it in a container with polars + the store mounted read-only::

    docker exec -w /app feature-computer python ops/underrepresented_tickers.py --store /store
    docker exec -w /app feature-computer python ops/underrepresented_tickers.py --store /store --json --top 50

Output is human-readable text by default, or ``--json`` for a machine-readable blob.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ticker_coverage import (
    DEFAULT_STORE_ROOT,
    PartitionStoreReader,
    StoreReader,
    _stream_window_dates,
)

# The live gap only needs who is SETTLED now and who is LIVE now, so both passes read a bounded recent slice:
#   * RECENT backfill dates establish the present-day settled symbol set (every currently-traded name appears
#     on the latest settled days; a deeper read finds nothing extra and costs more).
#   * The STREAM window is where the live feed lives (the stream only spans the last few captured days).
DEFAULT_BACKFILL_DATES = 3
DEFAULT_STREAM_WINDOW_DAYS = 7

# How many ranked symbols the text report shows by default (the JSON carries the full list).
DEFAULT_TOP = 40


@dataclass
class SymbolGap:
    """One symbol's universe-wide streaming gap: the groups it is settled-in-backfill but stream-absent in."""

    symbol: str
    stream_groups: set[str] = field(default_factory=set)
    backfill_groups: set[str] = field(default_factory=set)

    @property
    def under_rep_groups(self) -> set[str]:
        """Groups the symbol is settled in backfill but absent from the live stream — the FP_TICK_SYMBOLS gap."""
        return self.backfill_groups - self.stream_groups

    @property
    def under_rep_score(self) -> int:
        return len(self.under_rep_groups)

    @property
    def fully_streamed(self) -> bool:
        """True when every group the symbol is backfilled in is also streaming (no live gap)."""
        return not self.under_rep_groups


def _recent_backfill_dates(reader: StoreReader, group: str, version: str, n_dates: int) -> list[str]:
    """The most recent ``n_dates`` settled backfill dates for a group (present-day settled set)."""
    dates = reader.partition_dates(group, version, "backfill")
    return dates[-n_dates:] if n_dates > 0 else dates


def build_symbol_gaps(
    reader: StoreReader,
    backfill_dates: int = DEFAULT_BACKFILL_DATES,
    stream_days: int = DEFAULT_STREAM_WINDOW_DAYS,
) -> dict[str, SymbolGap]:
    """Walk every group ONCE, accumulating per-symbol stream/backfill group membership over the bounded recent
    slice. Pure over the reader, so it is unit-tested with an in-memory fake store. One universe pass — every
    partition's symbol set is read once and scattered to the symbols it contains (not re-read per symbol)."""
    gaps: dict[str, SymbolGap] = {}

    def get(symbol: str) -> SymbolGap:
        if symbol not in gaps:
            gaps[symbol] = SymbolGap(symbol=symbol)
        return gaps[symbol]

    for group in reader.list_groups():
        version = reader.group_version(group)
        if version is None:
            continue

        stream_dates = _stream_window_dates(reader.partition_dates(group, version, "stream"), stream_days)
        for date_iso in stream_dates:
            for symbol in reader.symbols_on_date(group, version, "stream", date_iso):
                get(symbol).stream_groups.add(group)

        for date_iso in _recent_backfill_dates(reader, group, version, backfill_dates):
            for symbol in reader.symbols_on_date(group, version, "backfill", date_iso):
                get(symbol).backfill_groups.add(group)

    return gaps


def rank_under_represented(gaps: dict[str, SymbolGap]) -> list[SymbolGap]:
    """Symbols with a non-empty live gap, ranked by score (most under-streamed first), then by symbol."""
    flagged = [gap for gap in gaps.values() if gap.under_rep_score > 0]
    flagged.sort(key=lambda gap: (-gap.under_rep_score, gap.symbol))
    return flagged


def per_group_gap_tally(gaps: dict[str, SymbolGap]) -> dict[str, int]:
    """{group: how many symbols are settled in it but not streaming} — which groups leak the most symbols."""
    tally: dict[str, int] = {}
    for gap in gaps.values():
        for group in gap.under_rep_groups:
            tally[group] = tally.get(group, 0) + 1
    return tally


def build_report(
    reader: StoreReader,
    backfill_dates: int = DEFAULT_BACKFILL_DATES,
    stream_days: int = DEFAULT_STREAM_WINDOW_DAYS,
) -> dict[str, object]:
    """The full universe under-representation report as a JSON-able dict. Pure over the reader, so the whole
    assembly is unit-tested without a store."""
    gaps = build_symbol_gaps(reader, backfill_dates=backfill_dates, stream_days=stream_days)
    flagged = rank_under_represented(gaps)
    tally = per_group_gap_tally(gaps)

    n_backfilled = sum(1 for gap in gaps.values() if gap.backfill_groups)
    n_streamed = sum(1 for gap in gaps.values() if gap.stream_groups)

    return {
        "n_symbols_seen": len(gaps),
        "n_symbols_backfilled": n_backfilled,
        "n_symbols_streamed": n_streamed,
        "n_symbols_under_represented": len(flagged),
        "backfill_dates_sampled": backfill_dates,
        "stream_window_days": stream_days,
        "per_group_gap": dict(sorted(tally.items(), key=lambda item: (-item[1], item[0]))),
        "under_represented": [
            {
                "symbol": gap.symbol,
                "under_rep_score": gap.under_rep_score,
                "n_backfill_groups": len(gap.backfill_groups),
                "n_stream_groups": len(gap.stream_groups),
                "under_rep_groups": sorted(gap.under_rep_groups),
            }
            for gap in flagged
        ],
    }


def render_text(report: dict[str, object], top: int = DEFAULT_TOP) -> str:
    lines: list[str] = []
    lines.append("UNDER-REPRESENTED TICKERS — settled in backfill but absent from the live stream")
    lines.append("=" * 78)
    lines.append(
        f"symbols seen: {report['n_symbols_seen']}  ·  backfilled: {report['n_symbols_backfilled']}  ·  "
        f"streamed: {report['n_symbols_streamed']}  ·  under-represented: "
        f"{report['n_symbols_under_represented']}"
    )
    lines.append(
        f"(window: {report['backfill_dates_sampled']} recent backfill dates · "
        f"{report['stream_window_days']}-day stream)"
    )
    lines.append("")

    per_group = report["per_group_gap"]
    if isinstance(per_group, dict) and per_group:
        lines.append("GROUPS LEAKING THE MOST SYMBOLS (settled-not-streaming count):")
        for group, count in list(per_group.items())[:15]:
            lines.append(f"  {str(group):28} {count}")
        lines.append("")

    flagged = report["under_represented"]
    if isinstance(flagged, list):
        lines.append(f"TOP UNDER-STREAMED SYMBOLS (showing {min(top, len(flagged))} of {len(flagged)}):")
        lines.append(f"  {'symbol':12} {'score':6} {'bf':5} {'stream':7} missing-groups")
        for entry in flagged[:top]:
            assert isinstance(entry, dict)
            missing = ", ".join(str(group) for group in entry["under_rep_groups"][:6])
            groups_list = entry["under_rep_groups"]
            assert isinstance(groups_list, list)
            if len(groups_list) > 6:
                missing += f", +{len(groups_list) - 6} more"
            lines.append(
                f"  {str(entry['symbol']):12} {entry['under_rep_score']:<6} "
                f"{entry['n_backfill_groups']:<5} {entry['n_stream_groups']:<7} {missing}"
            )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universe-wide under-represented-ticker flag (backfill-present but stream-absent)."
    )
    parser.add_argument(
        "--store", default=DEFAULT_STORE_ROOT, help="Feature-store root (default $STORE_ROOT)."
    )
    parser.add_argument(
        "--backfill-dates",
        type=int,
        default=DEFAULT_BACKFILL_DATES,
        help="How many recent settled backfill dates to read for the present-day settled set.",
    )
    parser.add_argument(
        "--stream-days",
        type=int,
        default=DEFAULT_STREAM_WINDOW_DAYS,
        help="Stream-window length in days for the live-presence read.",
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help="How many ranked symbols the text shows."
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable JSON report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not Path(args.store).exists():
        print(f"store root not found: {args.store}", file=sys.stderr)
        return 2
    reader = PartitionStoreReader(args.store)
    report = build_report(reader, backfill_dates=args.backfill_dates, stream_days=args.stream_days)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
