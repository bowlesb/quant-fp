"""Parity gate: the Class-A groups migrated onto ``DailySnapshotGroup`` emit cell-identical output.

The overhaul (docs/FEATURE_PREP_OVERHAUL.md) collapses the Class-A groups' hand-rolled four-method
boilerplate (``_compute_daily`` + ``_daily`` cache wrapper + ``compute`` broadcast + ``compute_latest``
latest-broadcast) onto one engine-owned base (``DailySnapshotGroup``), leaving each group to write ONLY its
per-(symbol, date) ``daily_snapshot`` math. This is a pure refactor — it MUST be value-identical.

These tests pin that: the migrated groups (Stage 0 POC: ``multi_day_returns`` / ``multi_day_vwap``; Stage 1:
``daily_beta`` / ``overnight_beta`` / ``overnight_intraday_split`` / ``liquidity_rank``) ride the shared base
with the cache/broadcast/split INHERITED, and emit the latest minute equal to ``compute()``'s last row. The
generic ``test_fp_latest`` proves ``compute_latest == compute().filter(T)`` for every group; the
``test_parity_stage1`` script (committed evidence in the PR) proves cell-identity vs the origin/main
four-method versions. ``liquidity_rank``'s universe-witness override (the one multi-input snapshot) gets its
own test here, since the standard frames carry no universe.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl

import pytest

from quantlib.features import BatchContext, REGISTRY
from quantlib.features.base import FeatureSpec, FeatureType
from quantlib.features.daily_snapshot_group import DailySnapshotGroup
from quantlib.features.groups.liquidity_rank import LiquidityRankGroup
from quantlib.features.groups.prior_day import PriorDayGroup
from quantlib.features.profile import build_frames

MIGRATED_SNAPSHOT_GROUPS = [
    "multi_day_returns",  # A.1 pure-broadcast
    "multi_day_vwap",  # A.1
    "daily_beta",  # A.1
    "overnight_beta",  # A.1
    "overnight_intraday_split",  # A.1
    "liquidity_rank",  # A.1 + universe-witness override
    "prior_day",  # A.2 snapshot-levels + at-T close via broadcast_exprs
]


@pytest.fixture(scope="module")
def ctx() -> BatchContext:
    frames = build_frames(n_tickers=40, window_min=250, daily_days=60)
    return BatchContext(frames=frames)


@pytest.mark.parametrize("group_name", MIGRATED_SNAPSHOT_GROUPS)
def test_migrated_group_is_a_daily_snapshot_group(group_name: str) -> None:
    """Every migrated group rides the shared base — it declares only its per-(symbol,date) math; the
    cache/broadcast/live-backfill split are INHERITED (not overridden)."""
    group = REGISTRY.get_group(group_name)
    assert isinstance(group, DailySnapshotGroup)
    assert type(group).daily_snapshot is not DailySnapshotGroup.daily_snapshot
    assert type(group).compute is DailySnapshotGroup.compute
    assert type(group).compute_latest is DailySnapshotGroup.compute_latest


@pytest.mark.parametrize("group_name", MIGRATED_SNAPSHOT_GROUPS)
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


def test_liquidity_rank_universe_witness_path(ctx: BatchContext) -> None:
    """liquidity_rank is the one multi-input snapshot group: its rank denominator depends on the universe
    membership, paired into the cache witness via the ``_snapshot_witness`` override. The standard frames carry
    no universe, so exercise the universe-PRESENT path explicitly at the snapshot level (where the rank is
    observable — the broadcast LEFT-join can null it out on these synthetic dates). A narrower universe must
    (a) re-key the cache witness and (b) re-rank within only its members — a different, correct result —
    proving the override is live and never stale-serves across a changed membership."""
    daily = ctx.frame("daily")
    all_symbols = daily.select("symbol").unique().sort("symbol")
    half = all_symbols.head(all_symbols.height // 2)

    ctx_full = BatchContext(frames={**ctx.frames, "universe": all_symbols})
    ctx_half = BatchContext(frames={**ctx.frames, "universe": half})

    # The witness differs across the two universes (so the cache cannot collide them).
    group = LiquidityRankGroup()
    assert group._snapshot_witness(daily, ctx_full) != group._snapshot_witness(daily, ctx_half)

    # The snapshot itself re-ranks within the narrowed member set: fewer rows (inner join to members) and a
    # different rank denominator, so the per-member ranks change (a real value effect of the witness input).
    snap_full = LiquidityRankGroup().daily_snapshot(daily, ctx_full).sort(["symbol", "date"])
    snap_half = LiquidityRankGroup().daily_snapshot(daily, ctx_half).sort(["symbol", "date"])
    assert snap_full.height > snap_half.height  # narrowed universe drops the excluded members' rows
    shared = half["symbol"].to_list()
    full_shared = snap_full.filter(pl.col("symbol").is_in(shared)).select("symbol", "date", "liquidity_rank")
    half_shared = snap_half.filter(pl.col("symbol").is_in(shared)).select("symbol", "date", "liquidity_rank")
    assert not full_shared.equals(half_shared)  # same members, different denominator -> different ranks

    # The per-instance cache is consulted by witness: re-keying the SAME instance recomputes (no stale serve).
    instance = LiquidityRankGroup()
    first, _ = instance._daily(ctx_full)
    second, _ = instance._daily(ctx_half)
    assert not first.sort(["symbol", "date"]).equals(second.sort(["symbol", "date"]))


def test_prior_day_a2_broadcast_exprs_path() -> None:
    """prior_day is the A.2 sub-shape: the snapshot holds per-(symbol,date) LEVELS and the features mix a level
    with the at-T minute close (dist_from_prior_close = close/prev_close - 1) via broadcast_exprs +
    minute_columns. On a hand-built frame with a real prior-day bar (the standard synthetic frames null these),
    assert the at-T close flows into the close-relative feature while the pure-level gap_open does not."""
    group = PriorDayGroup()
    assert group.broadcast_exprs() is not None  # A.2 hook live
    assert group.minute_columns == ("close",)  # at-T close carried into the broadcast

    # D-1 sets the prior-day levels (prev_close=100); D carries two intraday minutes at different closes.
    daily = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": [date(2026, 6, 11), date(2026, 6, 12)],
            "open": [99.0, 102.0],
            "high": [101.0, 103.0],
            "low": [98.0, 101.0],
            "close": [100.0, 102.5],
        }
    )
    minute_agg = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "minute": [
                datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 12, 14, 1, tzinfo=timezone.utc),
            ],
            "close": [110.0, 121.0],  # two different at-T closes on day D
        }
    )
    out = group.compute(BatchContext(frames={"daily": daily, "minute_agg": minute_agg})).sort("minute")
    # dist_from_prior_close = close/prev_close - 1, prev_close=100 -> 0.10 then 0.21 (tracks the at-T close).
    assert out["dist_from_prior_close"].to_list() == pytest.approx([0.10, 0.21])
    # gap_open = open[D]/close[D-1] - 1 = 102/100 - 1 = 0.02, a pure LEVEL, identical at both minutes.
    assert out["gap_open"].to_list() == pytest.approx([0.02, 0.02])


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

    def daily_snapshot(self, source: pl.DataFrame, ctx: BatchContext) -> pl.DataFrame:
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
