"""Materialize features into the Parquet store (the write path).

Computes every runnable group for a day + source and writes each to its source-tagged partition.
Same code serves the backfill write (``source=backfill``) and, fed live frames, the live write
(``source=stream``) — so what's stored is parity-true by construction.

Usage: python -m quantlib.features.materialize <root> <YYYY-MM-DD> <stream|backfill>
"""

from __future__ import annotations

import sys

from quantlib.features import store
from quantlib.features.backfill_bars import (
    backfill_bars,
    backfill_daily,
    tradable_universe,
)
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.engine import run_group
from quantlib.features.loaders import load_filings, load_minute_agg, load_reference
from quantlib.features.raw_loaders import (
    load_raw_minute_agg,
    load_raw_tick_enriched_minute_agg,
    load_raw_trades,
)
from quantlib.features.reduction_anchor import attach_reduction_anchors

DEFAULT_RAW_ROOT = "/store"


def _write_all(
    root: str,
    day: str,
    source: str,
    frames: dict,
    only_groups: list[str] | None = None,
    shard: int | None = None,
) -> int:
    """Compute + write every runnable group for ``day``. ``only_groups`` scopes the write to a subset (the
    selective-backfill path: build the raw frames once, materialize JUST the requested groups) — None writes
    all runnable groups (the full materialize). Each (group, source, date) partition is written atomically.

    ``shard`` names the output file ``data-<shard>.parquet`` instead of the single ``data.parquet``, so
    DISJOINT symbol batches written to the SAME (group, source, date) partition UNION on read instead of
    clobbering each other (the chunked-sweep path: each 200-symbol chunk is a shard). ``shard=None`` writes
    the single ``data.parquet`` (whole-partition backfill / repair) — unchanged behaviour.
    """
    frames = attach_reduction_anchors(frames)
    ctx = BatchContext(frames=frames)
    for group in runnable(frames):
        if only_groups is not None and group.name not in only_groups:
            continue
        out = run_group(group, ctx, validate=False)
        store.write_group(root, group.name, group.version, source, day, out, shard=shard)
        print(f"wrote group={group.name} source={source} date={day}: {out.height} rows")
    return (
        frames["minute_agg"]["symbol"].n_unique() if frames["minute_agg"].height else 0
    )


def materialize_alpaca_bars(root: str, day: str, symbols: list[str]) -> int:
    """Backfill bars for ANY symbols directly from Alpaca and write the bar features. Also loads the
    DAILY history so the multi-day features compute + broadcast (the full minute + daily horizon
    set). Returns the symbol count materialized."""
    frames = {
        "minute_agg": backfill_bars(day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    return _write_all(root, day, "backfill", frames)


def materialize_from_raw(
    root: str, raw_root: str, day: str, symbols: list[str], shard: int | None = None
) -> int:
    """Materialize bar features by READING the already-downloaded ``/store/raw`` minute bars instead of
    re-fetching them from Alpaca — the MATERIALIZE stage of the acquire/materialize segregation. The
    raw minute bars come from ``load_raw_minute_agg`` (download-once); ``daily`` still comes from
    ``backfill_daily`` (daily history is NOT in ``/store/raw``) and ``reference`` from the DB. ``shard``
    (the chunked-sweep batch index) writes ``data-<shard>.parquet`` so disjoint chunks union on read.
    Returns the symbol count materialized."""
    frames = {
        "minute_agg": load_raw_minute_agg(raw_root, day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    return _write_all(root, day, "backfill", frames, shard=shard)


def materialize_from_raw_full(
    root: str, raw_root: str, day: str, symbols: list[str], shard: int | None = None
) -> int:
    """Like ``materialize_from_raw`` but ALSO reads ``/store/raw/trades`` + ``/store/raw/quotes`` and
    enriches ``minute_agg`` with the per-minute tick columns (n_trades, signed_volume, spread, imbalance,
    book sizes) and supplies the per-trade ``trades`` frame — so the ORDER-FLOW groups (trade_flow,
    quote_spread, liquidity, signed_trade_ratio, tick_runlength, microstructure_burst) become runnable and
    write a backfill side. This is the materialize the parity sweep needs to validate the tick/quote
    features; the bar-only ``materialize_from_raw`` cannot produce them. ``shard`` (the chunked-sweep batch
    index) writes ``data-<shard>.parquet`` so disjoint chunks union on read. Returns the symbol count.
    """
    bars = load_raw_minute_agg(raw_root, day, symbols)
    frames = {
        "minute_agg": load_raw_tick_enriched_minute_agg(raw_root, day, symbols, bars),
        "trades": load_raw_trades(raw_root, day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    return _write_all(root, day, "backfill", frames, shard=shard)


def materialize_from_raw_bar_groups(
    root: str, raw_root: str, day: str, symbols: list[str], only_groups: list[str]
) -> int:
    """Bar-only from-raw materialize for ``only_groups`` over ALL ``symbols`` in a SINGLE compute (no
    chunking, no tick read). This is the materialize the cross-sectional UNIVERSE-REDUCE groups (breadth,
    cross_sectional_rank, ...) need: their per-minute value is a reduction over the WHOLE symbol set present
    that minute, so the compute MUST see every symbol at once — a chunked materialize would compute a
    SEPARATE partial-universe reduction per chunk (a 500-symbol breadth, not a full-universe one) and the
    full-universe live stream could never match it. Bars-only (close/volume) is all these groups read, so
    this skips the expensive trades/quotes tape. Returns the symbol count materialized."""
    frames = {
        "minute_agg": load_raw_minute_agg(raw_root, day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    return _write_all(root, day, "backfill", frames, only_groups=only_groups)


def materialize_from_raw_groups(
    root: str,
    raw_root: str,
    day: str,
    symbols: list[str],
    only_groups: list[str],
    shard: int | None = None,
) -> int:
    """Selective from-raw materialize: build the SAME full-tick ``/store/raw`` frames as
    ``materialize_from_raw_full`` (so bar AND order-flow groups are runnable), but WRITE only the partitions
    for ``only_groups``. This is the per-group on-ramp for the findings->features loop — give a NEW feature's
    group historical coverage without recomputing the other ~600 features. Returns the symbol count.

    ``shard`` (a symbol-batch index) writes ``data-<shard>.parquet`` so disjoint symbol chunks UNION on read
    instead of clobbering each other — the memory-bounded driver materializes a full-universe day as a series
    of symbol chunks (each chunk loads only its own raw frame, capping peak RAM) that union to the whole day.
    Only-bar groups read just ``minute_agg`` so per-symbol chunking is exact (no cross-symbol reduction); the
    cross-sectional universe-reduce groups must NOT be sharded this way (use ``materialize_from_raw_bar_groups``).
    ``shard=None`` writes the single ``data.parquet`` (whole-partition) — unchanged behaviour.
    """
    bars = load_raw_minute_agg(raw_root, day, symbols)
    frames = {
        "minute_agg": load_raw_tick_enriched_minute_agg(raw_root, day, symbols, bars),
        "trades": load_raw_trades(raw_root, day, symbols),
        "daily": backfill_daily(day, symbols),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    return _write_all(root, day, "backfill", frames, only_groups=only_groups, shard=shard)


def materialize_minute(
    root: str, day: str, source: str, only_groups: list[str] | None = None
) -> None:
    """Compute and write minute-aggregate groups for a day + source. ``only_groups`` scopes the work
    to specific groups — the REPAIR path: fix feature Y over period X by re-materializing only its
    group for those dates. Each (group, source, date) partition is independent, so a repair fans out
    in parallel across dates/groups with no contention and no global lock (atomic per partition).
    """
    frames = {
        "minute_agg": load_minute_agg(day, source),
        "reference": load_reference(),
        "filings": load_filings(day),
    }
    ctx = BatchContext(frames=frames)
    groups = [
        g for g in runnable(frames) if only_groups is None or g.name in only_groups
    ]
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
        print(
            f"materialized {count} symbols from Alpaca for {day} (requested {len(symbols)})"
        )
        return
    if args and args[0] == "raw":
        # raw <root> <day> <n_symbols> [raw_root]: read /store/raw minute bars (download-once) and write
        root, day, n = args[1], args[2], int(args[3])
        raw_root = args[4] if len(args) > 4 else DEFAULT_RAW_ROOT
        symbols = tradable_universe(limit=n)
        count = materialize_from_raw(root, raw_root, day, symbols)
        print(
            f"materialized {count} symbols from {raw_root}/raw for {day} (requested {len(symbols)})"
        )
        return
    if len(args) < 3:
        raise SystemExit(
            "usage: materialize <root> <day> <stream|backfill> [group,..]"
            "  |  materialize alpaca <root> <day> <n>"
            "  |  materialize raw <root> <day> <n> [raw_root]"
        )
    only = args[3].split(",") if len(args) > 3 else None
    materialize_minute(args[0], args[1], args[2], only)


if __name__ == "__main__":
    main()
