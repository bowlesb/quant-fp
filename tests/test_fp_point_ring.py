"""``PointRing`` parity: the production carried-state replacement for ``resolve_points`` reproduces it
byte-identically, including on SPARSE symbols (the positional-not-epoch lag invariant). Mirrors the reference
gate in test_fp_points_carried_parity.py, but against the real ``PointRing`` numpy state the engine will fold.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, resolve_points
from quantlib.features.point_ring import (
    PointRing,
    PointSpec,
    point_frame_from_ring,
    point_specs,
    shift_lags,
)

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _sparse_stream(n_sym: int, n_min: int, gap_period: int, gap_fraction: float, seed: int) -> pl.DataFrame:
    """Bar stream where a ``gap_fraction`` slice of symbols is missing every ``gap_period``-th minute (so their
    positional row-lag and time-lag diverge). Every symbol present at minute 0."""
    rng = np.random.default_rng(seed)
    symbols = [f"S{i:03d}" for i in range(n_sym)]
    gap_syms = set(symbols[: int(n_sym * gap_fraction)])
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(n_min)]
    rows: list[dict[str, object]] = []
    for mi, minute in enumerate(minutes):
        gapped = mi > 0 and mi % gap_period == 0
        for si, symbol in enumerate(symbols):
            if gapped and symbol in gap_syms:
                continue
            base_price = 100.0 + si + mi * 0.1
            rows.append(
                {
                    "symbol": symbol,
                    "minute": minute,
                    "close": base_price,
                    "high": base_price + 0.5 + rng.random() * 0.1,
                    "low": base_price - 0.5 - rng.random() * 0.1,
                    "volume": 1000.0 + si * 10 + mi,
                }
            )
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
        .sort(["symbol", "minute"])
    )


def _lag_groups(stream: pl.DataFrame) -> list[ReductionGroup]:
    """Runnable reduction groups on ``minute_agg`` carrying at least one positive-lag point."""
    groups: list[ReductionGroup] = []
    for group in runnable({"minute_agg": stream}):
        if isinstance(group, ReductionGroup) and any(
            any(k > 0 for k in shift_lags(expr)) for expr in group.points().values()
        ):
            groups.append(group)
    return groups


def _resolve_via_ring(
    groups: list[ReductionGroup], stream: pl.DataFrame, symbols: list[str]
) -> pl.DataFrame:
    """Fold ``stream`` minute-by-minute through ONE shared ``PointRing`` (the live-engine layout) and rebuild
    the latest-row ``__pt_`` frame via the production ``point_frame_from_ring`` — the exact path the engine
    wiring uses, so this test gates the real code, not a test-only reconstruction."""
    ring = PointRing(symbols, point_specs(groups))
    minutes = sorted(stream["minute"].unique())
    for minute in minutes:
        ring.fold(stream.filter(pl.col("minute") == minute))
    return point_frame_from_ring(groups, ring, symbols, minutes[-1])


def _assert_equal(truth: pl.DataFrame, ring: pl.DataFrame, label: str) -> None:
    point_cols = [c for c in truth.columns if c.startswith("__pt_")]
    truth = truth.sort("symbol").select(["symbol", *point_cols])
    ring = ring.sort("symbol").select(["symbol", *point_cols])
    assert truth["symbol"].to_list() == ring["symbol"].to_list(), f"{label}: symbol set differs"
    for col in point_cols:
        a = truth[col].to_numpy().astype(np.float64)
        b = ring[col].to_numpy().astype(np.float64)
        both_nan = np.isnan(a) & np.isnan(b)
        close = np.isclose(a, b, rtol=0.0, atol=1e-12, equal_nan=False)
        bad = ~(both_nan | close)
        assert not bad.any(), (
            f"{label}.{col}: {int(bad.sum())} mismatches\n"
            f"  truth={a[bad][:5]} ring={b[bad][:5]} symbols={np.array(truth['symbol'])[bad][:5]}"
        )


def test_point_ring_matches_resolve_points_sparse() -> None:
    """ONE shared PointRing + point_frame_from_ring == resolve_points byte-identical on a sparse fixture,
    over the LAG-carrying groups (the only ones whose carried form is non-trivial), including gap symbols."""
    stream = _sparse_stream(n_sym=12, n_min=150, gap_period=7, gap_fraction=0.5, seed=13)
    groups = _lag_groups(stream)
    assert groups, "expected lag-carrying groups (efficiency/return_dynamics/momentum_consistency)"
    symbols = sorted(stream["symbol"].unique().to_list())
    latest = sorted(stream["minute"].unique())[-1]
    truth = resolve_points(groups, stream, latest)
    _assert_equal(truth, _resolve_via_ring(groups, stream, symbols), "sparse:lag-groups")


def test_point_ring_matches_resolve_points_all_groups_dense() -> None:
    """The full live layout: ONE shared PointRing across ALL runnable reduction groups (at-T + lag points)
    reproduces resolve_points' entire latest_frame — the exact frame assemble_from_long consumes."""
    from quantlib.features.profile import build_frames, runs_incremental

    frames = build_frames(20, 130, 250, include_trades=True)
    groups = [g for g in runnable(frames) if isinstance(g, ReductionGroup) and runs_incremental(g)]
    stream = frames["minute_agg"]
    symbols = sorted(stream["symbol"].unique().to_list())
    latest = sorted(stream["minute"].unique())[-1]
    truth = resolve_points(groups, stream, latest)
    _assert_equal(truth, _resolve_via_ring(groups, stream, symbols), "dense:all-groups")


def test_point_ring_lag_is_positional_not_time() -> None:
    """Direct invariant: on a gapped single symbol, lag(close, 3) is the 3rd prior PRESENT bar (positional),
    NOT the bar 3 minutes ago (which a time-keyed ring would return / NaN)."""
    present = [0, 1, 2, 5, 6, 7]  # minutes 3,4 absent
    closes = [100.0, 101.0, 102.0, 105.0, 106.0, 107.0]
    rows = [
        {"symbol": "S000", "minute": BASE + dt.timedelta(minutes=m), "close": c}
        for m, c in zip(present, closes)
    ]
    stream = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    ring = PointRing(["S000"], [PointSpec("l3", "close", 3)])
    for minute in sorted(stream["minute"].unique()):
        ring.fold(stream.filter(pl.col("minute") == minute))
    # at minute 7 (6th present bar), the 3rd-prior PRESENT bar is minute 2 close = 102.0.
    assert ring.lag("close", 3)[0] == 102.0
