"""THE value-parity gate for the unified-state demolition.

The structure is about to be torn down — the ~7 overlapping state mechanisms (SessionCache,
WindowedSumState, ReductionFoldState, CumulativeState, EMA/Lag/Extrema kinds, PointRing) collapse into ONE
running-state primitive every group folds on. The structure changes radically; every group's emitted VALUES
must stay byte-identical. This gate is what catches a structural rewrite silently breaking a value.

For EVERY runnable group it asserts the LIVE stateful path (``_live_value`` — the live-fc dispatch driven
through the CORRECT streaming sequence: ``IncrementalEngine.seed(history)``+``step`` for an armed reduction,
``StatefulEngine.seed``+``step`` for a stateful group, the carried-ring / ``compute_latest`` otherwise) equals
the BACKFILL TRUTH (``compute().filter(last minute)`` — the rolling source of truth, always correct), within
each feature's declared tolerance. The churn-tolerant incremental/plain groups are gated on a GENUINELY-SPARSE
(gappy) fixture (where a structural state rewrite is most likely to break — membership churn / positional-lag
corners a dense fixture never exercises); the StatefulEngine asserts a stable symbol set per fold (it does not
absent-as-zero align — itself a finding the unified primitive should make uniform), so its groups are gated on
the dense fixture, their real per-fold regime.

It also pins the held-state fold-order invariant on sparse data (one-shot seed-then-fold == a seed/fold
boundary moved mid-stream) for the incremental groups — the contract a hot-swap reseed and the unified
primitive both rely on. The gate is the EXECUTABLE SPEC for the demolition: any new primitive must keep it
green; when it lands, point ``_live_value`` at it and this same gate certifies the values survived. NOTE:
today (pre-demolition) it certifies the CURRENT live path == backfill, so it doubles as a standing regression
gate.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import declarative
from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.compare import runnable
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.profile import (
    build_frames,
    reads_raw_trades,
    runs_incremental,
    thin_trades_to_live_breadth,
)
from quantlib.features.stateful import StatefulEngine, StatefulGroup

BASE = dt.datetime(2026, 6, 16, 13, 30, tzinfo=dt.timezone.utc)


def _make_sparse(
    frames: dict[str, pl.DataFrame], gap_period: int, gap_fraction: float
) -> dict[str, pl.DataFrame]:
    """Punch minute GAPS into ``minute_agg`` (and the raw ``trades`` tape, keyed by ``ts``): a
    ``gap_fraction`` slice of symbols is missing every ``gap_period``-th minute. Every symbol is present at
    minute 0 (warmup). The reference/daily snapshots are untouched (they carry no minute). Returns a new
    frames dict — the membership-churn + positional-lag regime the demolition must survive."""
    minute_agg = frames["minute_agg"]
    symbols = sorted(minute_agg["symbol"].unique().to_list())
    gap_syms = set(symbols[: int(len(symbols) * gap_fraction)])
    minutes = sorted(minute_agg["minute"].unique())
    minute_index = {minute: i for i, minute in enumerate(minutes)}

    def gapped(symbol: str, minute: object) -> bool:
        idx = minute_index[minute]
        return idx > 0 and idx % gap_period == 0 and symbol in gap_syms

    keep_minute = minute_agg.filter(
        ~pl.struct(["symbol", "minute"]).map_elements(
            lambda row: gapped(row["symbol"], row["minute"]), return_dtype=pl.Boolean
        )
    )
    out = dict(frames)
    out["minute_agg"] = keep_minute
    if "trades" in frames:
        # the tape's minute is ts truncated to 1m; drop the same (symbol, gapped-minute) ticks.
        trades = frames["trades"].with_columns(pl.col("ts").dt.truncate("1m").alias("_m"))
        out["trades"] = trades.filter(
            ~pl.struct(["symbol", "_m"]).map_elements(
                lambda row: row["_m"] in minute_index and gapped(row["symbol"], row["_m"]),
                return_dtype=pl.Boolean,
            )
        ).drop("_m")
    return out


def _live_value(group: FeatureGroup, frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """The group's latest-minute output via its TRUE LIVE STATEFUL path, driven through the CORRECT streaming
    sequence (seed the history strictly BEFORE the latest minute, then fold the latest minute) — NOT
    ``profile._live_call``, which seeds AND steps the SAME buffer (double-folding the latest minute; fine for
    latency timing, WRONG for values). This is the sequence the live fc actually runs and the one the unified
    primitive must reproduce.

      * armed ``incremental_safe`` reduction -> IncrementalEngine.seed(history) then .step(full)
      * StatefulGroup -> StatefulEngine.seed(history) then .step(full, hybrid_ctx)
      * raw-trades tick group -> compute_latest on a live-breadth-thinned tape (stateless, no sequence)
      * everything else -> compute_latest (stateless aggregate-at-T)
    """
    minute_agg = frames["minute_agg"]
    latest = minute_agg["minute"].max()
    history = {
        name: (frame.filter(pl.col("minute") < latest) if "minute" in frame.columns else frame)
        for name, frame in frames.items()
    }

    if runs_incremental(group):
        engine = IncrementalEngine([group])
        buffer_frame = frames[group.inputs[0].name]
        engine.seed(history[group.inputs[0].name])
        return engine.step(buffer_frame)[group.name]
    if isinstance(group, StatefulGroup):
        engine = StatefulEngine(group)
        buffer_frame = frames[group.inputs[0].name]
        ctx = BatchContext(frames=frames)
        hybrid_ctx = ctx if group.reduction_columns(ctx) is not None else None
        # Stateful groups are gated on a DENSE fixture (their live engine requires a stable symbol set per
        # fold — see the test's group-type split), so seed strictly before T then fold the latest minute once.
        engine.seed(history[group.inputs[0].name])
        return engine.step(buffer_frame, hybrid_ctx)
    if reads_raw_trades(group):
        ctx = BatchContext(frames=thin_trades_to_live_breadth(frames))
        return group.compute_latest(ctx)
    return group.compute_latest(BatchContext(frames=frames))


def _runnable_group_names() -> list[str]:
    frames = build_frames(n_tickers=24, window_min=250, daily_days=60)
    return sorted(g.name for g in runnable(frames))


def _assert_values_equal(truth: pl.DataFrame, live: pl.DataFrame, group, label: str) -> None:
    """Every feature column of ``live`` equals ``truth`` (compute().filter(last)) within the feature's declared
    tolerance, with NULL masks matching EXACTLY (a null-vs-value divergence is the most important parity break
    and must never be masked). Compares only the symbols present in BOTH at the latest minute."""
    feature_names = [c for c in truth.columns if c not in ("symbol", "minute")]
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    shared = set(truth["symbol"].to_list()) & set(live["symbol"].to_list())
    assert shared, f"{label}: no shared symbols between live and backfill at the latest minute"
    truth = truth.filter(pl.col("symbol").is_in(list(shared))).sort("symbol")
    live = live.filter(pl.col("symbol").is_in(list(shared))).sort("symbol").select(truth.columns)
    for feature in feature_names:
        tol = tolerances[feature]
        joined = truth.select("symbol", feature).join(
            live.select("symbol", pl.col(feature).alias("_a")), on="symbol"
        )
        bad = joined.filter(
            (pl.col(feature).is_null() != pl.col("_a").is_null())
            | (
                pl.col(feature).is_not_null()
                & pl.col("_a").is_not_null()
                & ((pl.col(feature) - pl.col("_a")).abs() > 1e-9 + tol * pl.col(feature).abs())
            )
        )
        assert (
            bad.height == 0
        ), f"{label}.{feature}: live != backfill on {bad.height} cells (tol={tol})\n{bad.head()}"


@pytest.mark.parametrize("rust_reduce", [False, True], ids=["FR0", "FR1"])
@pytest.mark.parametrize("group_name", _runnable_group_names())
def test_live_path_matches_backfill_on_sparse(
    group_name: str, rust_reduce: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The universal gate: for EVERY runnable group, the LIVE stateful path == the backfill truth
    (compute().filter(last)) on a genuinely-sparse fixture, within declared tolerance, at BOTH FR=0 and FR=1.
    This is the value contract the unified-primitive demolition must keep green. FR=1 (FP_RUST_REDUCE) arms the
    y-centered/rebase_time_axis conditioning the corr-denom + time-OLS reductions ride — a distinct numerical
    path that a FR=0-only gate would miss, so both are required for the full contract."""
    monkeypatch.setattr(declarative, "_USE_RUST_REDUCE", rust_reduce)
    dense = build_frames(n_tickers=24, window_min=250, daily_days=60)
    if group_name not in {g.name for g in runnable(dense)}:
        pytest.skip("group inputs not present in the standard test frames")
    dense_group = {g.name: g for g in runnable(dense)}[group_name]
    # A pure StatefulGroup's live engine (StatefulEngine) asserts a STABLE symbol set every fold — it does NOT
    # absent-as-zero align like the incremental engine; in production it RE-SEEDS on membership change (the
    # up_to_date/rebuild contract). So a gappy fold SEQUENCE is not its operating regime — it can't be driven
    # through one. Gate stateful groups on the DENSE fixture (their real per-fold regime); gate the
    # churn-tolerant incremental/plain groups on the SPARSE fixture (where a state rewrite is most likely to
    # break). This split is itself a finding: the unified primitive should make churn uniform across kinds.
    frames = (
        dense
        if isinstance(dense_group, StatefulGroup)
        else _make_sparse(dense, gap_period=9, gap_fraction=0.4)
    )
    runnable_now = {g.name: g for g in runnable(frames)}
    if group_name not in runnable_now:
        pytest.skip("group not runnable after sparsification")
    group = runnable_now[group_name]

    ctx = BatchContext(frames=frames)
    rolling = group.compute(ctx)
    latest = rolling["minute"].max()
    truth = rolling.filter(pl.col("minute") == latest).sort("symbol")

    live = _live_value(group, frames)
    if "minute" in live.columns:
        live = live.filter(pl.col("minute") == live["minute"].max())
    _assert_values_equal(truth, live, group, f"sparse:{group_name}")


def _incremental_group_names() -> list[str]:
    frames = build_frames(n_tickers=24, window_min=250, daily_days=60)
    return sorted(g.name for g in runnable(frames) if runs_incremental(g))


@pytest.mark.parametrize("group_name", _incremental_group_names())
def test_minute_by_minute_replay_equals_chunked_seed_sparse(group_name: str) -> None:
    """The held-state fold-order invariant on sparse data: folding the history in ONE seed then ``step`` the
    latest minute emits the SAME latest row as folding it as TWO chunks (an early seed + a mid ``step``) then
    the latest ``step``. Pins that the carried state has no order dependence across a re-seed boundary — the
    contract a hot-swap reseed and the unified primitive both rely on (a warm-started shard must match a
    freshly-seeded one). Both end at the IDENTICAL fold history; only WHERE the seed/fold boundary sits
    differs."""
    dense = build_frames(n_tickers=24, window_min=250, daily_days=60)
    if group_name not in {g.name for g in runnable(dense)}:
        pytest.skip("group inputs not present")
    frames = _make_sparse(dense, gap_period=9, gap_fraction=0.4)
    runnable_now = {g.name: g for g in runnable(frames)}
    if group_name not in runnable_now:
        pytest.skip("group not runnable after sparsification")
    group = runnable_now[group_name]

    buffer_frame = frames[group.inputs[0].name]
    minutes = sorted(buffer_frame["minute"].unique())
    prior, mid = minutes[-2], minutes[len(minutes) // 2]

    one_shot = IncrementalEngine([group])
    one_shot.seed(buffer_frame.filter(pl.col("minute") <= prior))
    a = one_shot.step(buffer_frame)[group.name].sort("symbol")

    # Same end-history, but the seed/fold boundary sits at ``mid``: seed <=mid, then fold each later minute
    # (mid+1 .. latest) one ``step`` at a time (``step`` folds only the frame's latest minute).
    chunked = IncrementalEngine([group])
    chunked.seed(buffer_frame.filter(pl.col("minute") <= mid))
    later = [m for m in minutes if m > mid]
    b = None
    for minute in later:
        b = chunked.step(buffer_frame.filter(pl.col("minute") <= minute))
    assert b is not None
    _assert_values_equal(a, b[group.name].sort("symbol"), group, f"replay:{group_name}")


def test_sparse_fixture_is_genuinely_gappy() -> None:
    """Anti-vacuity: the sparse fixture MUST actually drop bars (else every gate above is vacuous on dense
    data). Assert the gapped frame has materially fewer (symbol, minute) rows than the dense one."""
    dense = build_frames(n_tickers=24, window_min=250, daily_days=60)
    sparse = _make_sparse(dense, gap_period=9, gap_fraction=0.4)
    dense_rows = dense["minute_agg"].height
    sparse_rows = sparse["minute_agg"].height
    assert sparse_rows < dense_rows, "sparsifier dropped no bars — the gate would be vacuous"
    # at least a few hundred bars dropped (gap_period 9, 40% of symbols, 250 minutes)
    assert dense_rows - sparse_rows > 100, f"only {dense_rows - sparse_rows} bars dropped — too few to gate"
