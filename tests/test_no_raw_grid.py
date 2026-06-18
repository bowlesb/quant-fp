"""Unit + parity tests for the no-raw GRID materializer (``quantlib.features.no_raw_grid``).

Network/DB-free: the deterministic groups (calendar, calendar_events) read only ``(symbol, minute)``, so
we can build their inputs in-memory. The PARITY test is the load-bearing one: it asserts the grid path
produces byte-identical feature values to the raw path on the minutes BOTH produce — the grid merely fills
the extra minutes a bar never printed. Parity is by construction (both run the same ``group.compute`` on
the same ``minute`` timestamps), so this guards that the grid plumbing preserves it.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features import store
from quantlib.features.materialize import _write_all
from quantlib.features.no_raw_grid import (
    materialize_grid,
    minute_grid,
    no_raw_groups,
)
from quantlib.features.registry import REGISTRY
from quantlib.features.session import EXT_CLOSE_MINUTE, WARMUP_START_MINUTE, et_minute_of_day

DAY = "2026-06-12"  # a Friday (June -> EDT, UTC-4)


def _no_raw_group_objects() -> list:
    names = set(no_raw_groups())
    return [group for group in REGISTRY.groups() if group.name in names]


def test_no_raw_groups_are_the_pure_timestamp_groups() -> None:
    """The grid runs exactly the groups whose only input is the minute key — calendar + calendar_events —
    and never a group that needs bars/ticks/reference (those declare an input the grid does not satisfy)."""
    names = set(no_raw_groups())
    assert {"calendar", "calendar_events"} <= names
    for group in REGISTRY.groups():
        only_minute_key = all(
            spec.name == "minute_agg" and set(spec.columns) <= {"symbol", "minute"} for spec in group.inputs
        )
        assert (group.name in names) == only_minute_key, group.name


def test_minute_grid_spans_the_extended_session_per_symbol() -> None:
    """The grid is the cross of the symbols with every extended-session minute (08:00-20:00 ET), keyed
    exactly like minute_agg."""
    symbols = ["AAPL", "MSFT"]
    grid = minute_grid(DAY, symbols)
    assert grid.columns == ["symbol", "minute"]
    per_symbol = grid.group_by("symbol").len().sort("symbol")
    expected_minutes = EXT_CLOSE_MINUTE - WARMUP_START_MINUTE  # 1200 - 480 = 720 ext-session minutes
    assert per_symbol["len"].to_list() == [expected_minutes, expected_minutes]
    # every grid minute is inside the extended session [08:00, 20:00) ET
    etm = grid.select(et_minute_of_day(pl.col("minute")).alias("etm"))["etm"]
    assert etm.min() == WARMUP_START_MINUTE
    assert etm.max() == EXT_CLOSE_MINUTE - 1


def test_grid_values_match_raw_path_on_shared_minutes(tmp_path) -> None:
    """PARITY: for the minutes a bar printed, the grid path's deterministic-feature values equal the RAW
    path's — cell-for-cell. Both are written THROUGH the store (so both round to the same Float32 storage
    dtype, exactly as the real stream-vs-backfill parity grade is stored-vs-stored) and read back; the grid
    just also fills the no-bar minutes, which we exclude by intersecting."""
    symbols = ["AAPL", "MSFT"]
    # A handful of bar minutes spread across the session: pre-market, the open, mid-session, post-market.
    bar_minutes = [
        dt.datetime(2026, 6, 12, 12, 5, tzinfo=dt.timezone.utc),   # 08:05 ET pre-market
        dt.datetime(2026, 6, 12, 13, 30, tzinfo=dt.timezone.utc),  # 09:30 ET open
        dt.datetime(2026, 6, 12, 17, 0, tzinfo=dt.timezone.utc),   # 13:00 ET mid-session
        dt.datetime(2026, 6, 12, 23, 30, tzinfo=dt.timezone.utc),  # 19:30 ET post-market
    ]
    bar_minute_agg = pl.DataFrame(
        {
            "symbol": [sym for sym in symbols for _ in bar_minutes],
            "minute": [minute for _ in symbols for minute in bar_minutes],
        }
    ).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))

    only_groups = no_raw_groups()
    feature_names = [spec.name for group in _no_raw_group_objects() for spec in group.declare()]
    start = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 12, 23, 59, 59, tzinfo=dt.timezone.utc)

    # RAW path: write the deterministic groups from the bar minute_agg into one store root.
    raw_root = tmp_path / "raw_path"
    _write_all(str(raw_root), DAY, "backfill", {"minute_agg": bar_minute_agg}, only_groups=only_groups)
    raw_values = store.get_features(feature_names, symbols, start, end, str(raw_root), source="backfill")
    assert raw_values.height == len(symbols) * len(bar_minutes)

    # GRID path: materialize the same groups from the calendar grid into a SEPARATE store root.
    grid_root = tmp_path / "grid_path"
    materialize_grid(str(grid_root), DAY, symbols)
    grid_values = store.get_features(feature_names, symbols, start, end, str(grid_root), source="backfill")

    # Compare on the INTERSECTION of minutes (the grid is a superset — it also fills no-bar minutes).
    shared = raw_values.join(grid_values, on=["symbol", "minute"], how="inner", suffix="_grid")
    assert shared.height == raw_values.height  # every bar minute is present in the grid
    for name in feature_names:
        assert shared[name].to_list() == shared[f"{name}_grid"].to_list(), f"{name} diverged grid vs raw"


def test_materialize_grid_clears_stale_shard_before_whole_write(tmp_path) -> None:
    """A whole-partition (shard=None) grid write CLEANS the partition first: a stale sweep-SHARDED
    ``data-<chunk>.parquet`` left from a prior nightly sweep must NOT survive and UNION with the new
    ``data.parquet`` (the ``data*.parquet`` read glob unions both → double-counted symbols)."""
    symbols = ["AAPL"]
    partition = (
        tmp_path / "group=calendar" / "v=1.0.0" / "source=backfill" / f"date={DAY}"
    )
    partition.mkdir(parents=True)
    # A stale chunk file with a BOGUS symbol that must be gone after the clean rewrite.
    stale = pl.DataFrame(
        {
            "symbol": ["BOGUS"],
            "minute": [dt.datetime(2026, 6, 12, 13, 30, tzinfo=dt.timezone.utc)],
            "minute_of_day_et": [570.0],
        }
    ).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    stale.write_parquet(partition / "data-0.parquet")

    materialize_grid(str(tmp_path), DAY, symbols, only_groups=["calendar"])

    files = sorted(file.name for file in partition.glob("data*.parquet"))
    assert files == ["data.parquet"], f"stale shard not cleared: {files}"
    start = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 12, 23, 59, 59, tzinfo=dt.timezone.utc)
    written = store.get_features(
        ["minute_of_day_et"], ["AAPL", "BOGUS"], start, end, str(tmp_path), source="backfill"
    )
    # only the real symbol survives — the stale BOGUS row was cleared, not unioned
    assert set(written["symbol"].unique().to_list()) == {"AAPL"}


def test_materialize_grid_shard_write_preserves_siblings(tmp_path) -> None:
    """A SHARDED (shard set) grid write is an intentional per-chunk union and must NOT clear its
    siblings — disjoint symbol batches written as ``data-<chunk>.parquet`` union on read."""
    partition = (
        tmp_path / "group=calendar" / "v=1.0.0" / "source=backfill" / f"date={DAY}"
    )
    materialize_grid(str(tmp_path), DAY, ["AAPL"], only_groups=["calendar"], shard=0)
    materialize_grid(str(tmp_path), DAY, ["MSFT"], only_groups=["calendar"], shard=1)
    files = sorted(file.name for file in partition.glob("data*.parquet"))
    assert files == ["data-0.parquet", "data-1.parquet"]
    start = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 12, 23, 59, 59, tzinfo=dt.timezone.utc)
    written = store.get_features(
        ["minute_of_day_et"], ["AAPL", "MSFT"], start, end, str(tmp_path), source="backfill"
    )
    assert set(written["symbol"].unique().to_list()) == {"AAPL", "MSFT"}


def test_grid_is_a_superset_of_raw_minutes(tmp_path) -> None:
    """The grid fills minutes no bar printed — its reason for existing (deep coverage with no raw). So it
    has STRICTLY more (symbol, minute) rows than a sparse raw day, while agreeing on the shared ones."""
    symbols = ["AAPL"]
    materialize_grid(str(tmp_path), DAY, symbols)
    start = dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 12, 23, 59, 59, tzinfo=dt.timezone.utc)
    grid_values = store.get_features(["minute_of_day_et"], symbols, start, end, str(tmp_path), source="backfill")
    # 720 ext-session minutes for the one symbol — far more than a handful of printed bars.
    assert grid_values.height == EXT_CLOSE_MINUTE - WARMUP_START_MINUTE
    assert grid_values["minute_of_day_et"].is_not_null().all()
