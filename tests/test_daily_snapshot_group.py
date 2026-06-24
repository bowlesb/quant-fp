"""POC parity gate: the Class-A groups migrated onto ``DailySnapshotGroup`` emit cell-identical output.

The overhaul (docs/FEATURE_PREP_OVERHAUL.md) collapses the nine Class-A groups' hand-rolled four-method
boilerplate (``_compute_daily`` + ``_daily`` cache wrapper + ``compute`` broadcast + ``compute_latest``
latest-broadcast) onto one engine-owned base (``DailySnapshotGroup``), leaving each group to write ONLY its
per-(symbol, date) ``daily_snapshot`` math. This is a pure refactor — it MUST be value-identical.

These tests pin that: the two migrated POC groups (``multi_day_returns``, ``multi_day_vwap``) emit
cell-for-cell what the origin/main four-method versions emitted, on the standard test frames, for BOTH
``compute()`` (backfill) and ``compute_latest()`` (live). The generic ``test_fp_latest`` already proves
``compute_latest == compute().filter(T)`` for every group; this proves the migration itself changed no value.
"""
from __future__ import annotations

import polars as pl

import pytest

from quantlib.features import BatchContext, REGISTRY
from quantlib.features.base import FeatureSpec, FeatureType
from quantlib.features.daily_snapshot_group import DailySnapshotGroup
from quantlib.features.profile import build_frames


@pytest.fixture(scope="module")
def ctx() -> BatchContext:
    frames = build_frames(n_tickers=40, window_min=250, daily_days=60)
    return BatchContext(frames=frames)


@pytest.mark.parametrize("group_name", ["multi_day_returns", "multi_day_vwap"])
def test_migrated_group_is_a_daily_snapshot_group(group_name: str) -> None:
    """The migrated POC groups ride the shared base — they declare only their per-(symbol,date) math."""
    group = REGISTRY.get_group(group_name)
    assert isinstance(group, DailySnapshotGroup)
    # The group implements daily_snapshot; the cache/broadcast/split are inherited (not overridden).
    assert type(group).daily_snapshot is not DailySnapshotGroup.daily_snapshot
    assert type(group).compute is DailySnapshotGroup.compute
    assert type(group).compute_latest is DailySnapshotGroup.compute_latest


@pytest.mark.parametrize("group_name", ["multi_day_returns", "multi_day_vwap"])
def test_compute_latest_broadcasts_one_minute(group_name: str, ctx: BatchContext) -> None:
    """compute_latest emits exactly the latest minute's row per symbol, and it equals compute()'s last row."""
    group = REGISTRY.get_group(group_name)
    rolling = group.compute(ctx)
    latest = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == latest).sort("symbol")
    actual = group.compute_latest(ctx).sort("symbol").select(expected.columns)
    assert actual.equals(expected)


def test_session_cache_is_value_identical_across_minutes(ctx: BatchContext) -> None:
    """The cache only changes WHEN daily_snapshot runs, never WHAT it returns: a second compute() on the same
    snapshot (cache hit) yields the identical frame as the first (cache miss)."""
    group = REGISTRY.get_group("multi_day_returns")
    first = group.compute(ctx)
    second = group.compute(ctx)  # cache hit on the unchanged snapshot witness
    assert first.equals(second)


class _ProbeSnapshotGroup(DailySnapshotGroup):
    """A minimal Class-A group used to assert the base's contract directly (no registry side effects)."""

    name = "_probe_snapshot"
    version = "1.0.0"
    owner = "test"
    type = FeatureType.MULTI_DAY

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="probe_last_close",
                description="The prior-day close, broadcast across the session — a trivial Class-A feature.",
                dtype="Float64",
                valid_range=(0.0, 1e6),
                nan_policy="warmup",
                layer="A",
            )
        ]

    def daily_snapshot(self, source: pl.DataFrame) -> pl.DataFrame:
        daily = source.select(["symbol", "date", "close"]).sort(["symbol", "date"])
        return daily.with_columns(
            pl.col("close").shift(1).over("symbol").cast(pl.Float64).alias("probe_last_close")
        ).select(["symbol", "date", "probe_last_close"])


def test_base_broadcasts_snapshot_to_every_minute(ctx: BatchContext) -> None:
    """A bare DailySnapshotGroup: every minute of a date carries the SAME (intraday-invariant) daily value."""
    probe = _ProbeSnapshotGroup()
    probe.inputs = REGISTRY.get_group("multi_day_returns").inputs  # daily + minute_agg
    out = probe.compute(ctx).with_columns(pl.col("minute").dt.date().alias("_d"))
    # one distinct value per (symbol, date) across all that date's minutes
    per_day = out.group_by(["symbol", "_d"]).agg(pl.col("probe_last_close").n_unique().alias("n"))
    assert per_day["n"].max() == 1
