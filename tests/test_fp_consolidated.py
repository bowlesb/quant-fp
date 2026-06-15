"""Consolidated per-minute emit parity: the point-in-time + daily-broadcast families, computed in one
shared pass each, must equal each group's per-group compute_latest CELL-FOR-CELL (byte-identical).

This is a SCHEDULING change — the consolidated emitters apply the SAME column expressions on a shared
frame, so live/consolidated == compute_latest == compute().last must hold exactly (no tolerance). If it
diverged, the streaming write would disagree with the backfill the platform certifies.
"""
from __future__ import annotations

import polars as pl

import pytest

from quantlib.features import REGISTRY, BatchContext
from quantlib.features.consolidated import (
    DAILY_BROADCAST_GROUPS,
    POINT_IN_TIME_GROUPS,
    emit_daily_broadcast,
    emit_point_in_time,
)
from quantlib.features.profile import build_frames

SECTOR_CYCLE = (
    "Technology",
    "Healthcare",
    "Financial Services",
    "Energy",
    "Unmapped Bucket",
)


def _frames() -> dict[str, pl.DataFrame]:
    """Standard warm test frames, but with VARIED sector + flag values so the consolidated reference
    join is exercised on heterogeneous symbols (not a single constant), and the close varies so the
    round-level / prior-day close-relative columns take distinct values."""
    frames = build_frames(n_tickers=40, window_min=250, daily_days=60)
    reference = frames["reference"].with_columns(
        [
            pl.Series("sector", [SECTOR_CYCLE[i % len(SECTOR_CYCLE)] for i in range(frames["reference"].height)]),
            pl.Series("shortable", [i % 2 == 0 for i in range(frames["reference"].height)]),
            pl.Series("fractionable", [i % 3 == 0 for i in range(frames["reference"].height)]),
        ]
    )
    frames["reference"] = reference
    return frames


def _assert_byte_identical(actual: pl.DataFrame, expected: pl.DataFrame, group_name: str) -> None:
    actual = actual.sort("symbol").select(expected.columns)
    expected = expected.sort("symbol")
    assert actual.height == expected.height, f"{group_name}: row count {actual.height} != {expected.height}"
    assert actual.equals(expected), f"{group_name}: consolidated emit != compute_latest (not byte-identical)"


def test_point_in_time_consolidated_matches_per_group() -> None:
    frames = _frames()
    ctx = BatchContext(frames=frames)
    groups = [REGISTRY.get_group(name) for name in POINT_IN_TIME_GROUPS]
    consolidated = emit_point_in_time(groups, ctx)
    assert set(consolidated) == set(POINT_IN_TIME_GROUPS)
    for group in groups:
        _assert_byte_identical(consolidated[group.name], group.compute_latest(ctx), group.name)


def test_daily_broadcast_consolidated_matches_per_group() -> None:
    frames = _frames()
    ctx = BatchContext(frames=frames)
    groups = [REGISTRY.get_group(name) for name in DAILY_BROADCAST_GROUPS]
    consolidated = emit_daily_broadcast(groups, ctx)
    assert set(consolidated) == set(DAILY_BROADCAST_GROUPS)
    for group in groups:
        _assert_byte_identical(consolidated[group.name], group.compute_latest(ctx), group.name)


def test_point_in_time_consolidated_matches_rolling_last() -> None:
    """The consolidated emit must also equal the BACKFILL rolling form's last minute (the source of
    truth), closing the loop live/consolidated == compute_latest == compute().last."""
    frames = _frames()
    ctx = BatchContext(frames=frames)
    groups = [REGISTRY.get_group(name) for name in POINT_IN_TIME_GROUPS]
    consolidated = emit_point_in_time(groups, ctx)
    for group in groups:
        rolling = group.compute(ctx)
        last = rolling.filter(pl.col("minute") == rolling["minute"].max())
        _assert_byte_identical(consolidated[group.name], last, group.name)


def test_daily_broadcast_consolidated_matches_rolling_last() -> None:
    frames = _frames()
    ctx = BatchContext(frames=frames)
    groups = [REGISTRY.get_group(name) for name in DAILY_BROADCAST_GROUPS]
    consolidated = emit_daily_broadcast(groups, ctx)
    for group in groups:
        rolling = group.compute(ctx)
        last = rolling.filter(pl.col("minute") == rolling["minute"].max())
        _assert_byte_identical(consolidated[group.name], last, group.name)


@pytest.mark.parametrize("name", POINT_IN_TIME_GROUPS)
def test_point_in_time_subset_runnable(name: str) -> None:
    """A single point-in-time group in isolation still emits correctly through the shared pass (the
    consolidation must not depend on all five being present)."""
    frames = _frames()
    ctx = BatchContext(frames=frames)
    group = REGISTRY.get_group(name)
    out = emit_point_in_time([group], ctx)
    _assert_byte_identical(out[name], group.compute_latest(ctx), name)
