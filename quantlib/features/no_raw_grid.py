"""No-raw GRID materialize for the deterministic (pure-timestamp) feature groups.

The calendar / time-of-day groups (``calendar``, ``calendar_events``) are pure functions of the bar's
exchange timestamp — they declare ONLY ``minute_agg(symbol, minute)`` and read no price, volume, tick, or
reference data. ``materialize_from_raw`` still loads the ``/store/raw`` minute bars to materialize them, so
it needlessly reads the tape AND cannot go before the raw window (2024-12-11). Those features need none of
that: every value is decided by the ``minute`` alone.

This module materializes those groups from a (universe x trading-minute) GRID built from the trading
calendar only — no raw, arbitrarily far back, in seconds. Parity is by CONSTRUCTION, not by coincidence:
the grid is fed through the SAME ``_write_all`` -> ``runnable(frames)`` -> ``run_group`` -> ``group.compute``
path the raw materialize uses, with the SAME ``minute`` timestamps, so each deterministic group runs its
identical expressions and emits identical values. ``runnable`` over a minute-only ``frames`` dict selects
EXACTLY the groups whose inputs are satisfied by ``(symbol, minute)`` — the pure-timestamp groups — and
skips every group that needs bars/ticks/reference, so the selection is structural, not a hard-coded list.

The grid is a SUPERSET of the raw path's minutes (it fills every extended-session minute, not just the
minutes a bar happened to print), which is exactly its value for deep training coverage; on the minutes
both paths produce, the values are byte-identical (the parity test asserts this on the intersection).

Usage:
  python -m quantlib.features.no_raw_grid <root> <YYYY-MM-DD> <n_symbols|--symbols S1,S2,...>
"""

from __future__ import annotations

import sys

import polars as pl

from quantlib.features.backfill_bars import tradable_universe
from quantlib.features.base import KEY_COLUMNS
from quantlib.features.compare import runnable
from quantlib.features.materialize import _write_all
from quantlib.features.session import ext_session_minutes_utc
from quantlib.features.store import clear_backfill_groups_day

MINUTE_DTYPE = pl.Datetime("us", "UTC")  # the store key dtype (backfill_bars.BARS_SCHEMA["minute"])


def no_raw_groups() -> list[str]:
    """The group names runnable from a minute-only frame — the pure-timestamp groups a grid can emit.

    Derived from the registry via ``runnable`` over a ``{minute_agg: (symbol, minute)}`` frame, so any
    future pure-timestamp group is included automatically and any group needing bars/ticks/reference is
    excluded by construction (it declares an input the minute-only frame does not satisfy)."""
    probe = {"minute_agg": pl.DataFrame(schema={"symbol": pl.String, "minute": MINUTE_DTYPE})}
    return [group.name for group in runnable(probe)]


def minute_grid(day: str, symbols: list[str]) -> pl.DataFrame:
    """The (symbol x extended-session minute) grid for ``day`` keyed exactly like ``minute_agg``.

    One row per (symbol, minute) over every extended-session minute (08:00-20:00 ET), with the ``minute``
    column cast to the store key dtype. This is the minute-only ``minute_agg`` the deterministic groups
    consume — no price/volume columns, because they read none."""
    minutes = ext_session_minutes_utc(day).cast(MINUTE_DTYPE)
    grid = pl.DataFrame({"symbol": symbols}).join(
        pl.DataFrame({"minute": minutes}), how="cross"
    )
    return grid.select(list(KEY_COLUMNS)).sort(list(KEY_COLUMNS))


def materialize_grid(
    root: str, day: str, symbols: list[str], only_groups: list[str] | None = None, shard: int | None = None
) -> int:
    """Materialize the deterministic groups for ``symbols`` on ``day`` from the trading-calendar grid.

    Builds the minute-only grid and writes the pure-timestamp groups through the shared ``_write_all`` (the
    SAME compute+write path the raw materialize uses) into the backfill store. ``only_groups`` further
    scopes the write (default: every group the grid can run, i.e. ``no_raw_groups``); ``shard`` writes a
    per-chunk file so disjoint symbol batches union on read. Returns the symbol count materialized.
    """
    groups = only_groups if only_groups is not None else no_raw_groups()
    # On a whole-partition (shard=None) write, clear the target groups' backfill files for the day first so
    # it is a clean replace: the ``data*.parquet`` read glob would otherwise UNION a stale sweep-SHARDED
    # file (``data-<chunk>.parquet``) with the new ``data.parquet`` and double-count. A sharded write
    # (shard set) is an intentional per-chunk union and must NOT clear its siblings.
    if shard is None:
        clear_backfill_groups_day(root, day, groups)
    frames = {"minute_agg": minute_grid(day, symbols)}
    return _write_all(root, day, "backfill", frames, only_groups=groups, shard=shard)


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 3:
        raise SystemExit(
            "usage: python -m quantlib.features.no_raw_grid <root> <YYYY-MM-DD> "
            "<n_symbols | --symbols S1,S2,...>"
        )
    root, day, selector = args[0], args[1], args[2]
    if selector.startswith("--symbols"):
        raw = selector.split("=", 1)[1] if "=" in selector else args[3]
        symbols = [token.strip() for token in raw.split(",") if token.strip()]
    else:
        symbols = tradable_universe(limit=int(selector))
    count = materialize_grid(root, day, symbols)
    print(f"materialized {count} symbols x grid for {day} (groups: {', '.join(no_raw_groups())})")


if __name__ == "__main__":
    main()
