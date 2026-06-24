"""Cross-sectional gathers compute the latest-minute reduce over a BOUNDED trailing window, not the whole
buffer. peer_relative / cross_sectional_rank / return_dispersion each re-derived their windowed returns over
the ENTIRE minute buffer (a per-symbol time-join per window) and then discarded all but the latest minute.
Their ``compute_latest`` now uses ``compute_latest_on_window`` — the SAME ``compute`` on the buffer sliced to
the deepest return window — so the output is byte-identical while the per-minute work drops from O(buffer) to
O(max-window).

This gate proves the bounded form == the whole-buffer form INCLUDING on a SPARSE (gappy) universe, where a
symbol's bar ``w`` minutes ago may be absent. Their returns use the TIME-based ``lagged`` (as-of minute − w),
so the minute-window slice keeps exactly the bars each window reads — the sliced and whole-buffer reduces are
identical cell-for-cell. The generic ``test_fp_latest`` already guards ``compute_latest == compute().last``;
this adds the sparse-universe case the dense profiler fixture hides, plus the explicit before/after equality.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.cross_sectional_rank import RETURN_WINDOWS, CrossSectionalRankGroup
from quantlib.features.groups.peer_relative import PEER_WINDOWS, PeerRelativeReturnGroup
from quantlib.features.groups.return_dispersion import MINUTE_WINDOWS, ReturnDispersionGroup

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _sparse_minute_agg(
    n_sym: int, n_min: int, gap_period: int, gap_fraction: float, seed: int
) -> pl.DataFrame:
    """A gappy bar buffer: a ``gap_fraction`` slice of symbols is missing every ``gap_period``-th minute, so a
    time-based return at lag w hits an absent bar for them (the case the whole-buffer-vs-slice equivalence must
    survive). Every symbol present at minute 0."""
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
            price = 100.0 + si + mi * 0.1 + rng.standard_normal() * 0.05
            rows.append({"symbol": symbol, "minute": minute, "close": price, "volume": 1000.0 + si + mi})
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
        .sort(["symbol", "minute"])
    )


def _assert_equal(old: pl.DataFrame, new: pl.DataFrame, label: str) -> None:
    cols = sorted(old.columns)
    old = old.sort("symbol").select(cols)
    new = new.sort("symbol").select(cols)
    assert old["symbol"].to_list() == new["symbol"].to_list(), f"{label}: symbol set differs"
    for col in cols:
        if col in ("symbol", "minute"):
            assert old[col].to_list() == new[col].to_list(), f"{label}.{col}"
            continue
        a = old[col].to_numpy().astype(np.float64)
        b = new[col].to_numpy().astype(np.float64)
        both_nan = np.isnan(a) & np.isnan(b)
        close = np.isclose(a, b, rtol=0.0, atol=1e-12, equal_nan=False)
        bad = ~(both_nan | close)
        assert (
            not bad.any()
        ), f"{label}.{col}: {int(bad.sum())} mismatches\n  whole={a[bad][:5]} bounded={b[bad][:5]}"


def _whole_buffer_latest(group, ctx: BatchContext) -> pl.DataFrame:
    """The OLD whole-buffer ``compute_latest``: run ``compute`` over the full buffer, filter to the latest
    minute — what the bounded ``compute_latest_on_window`` form must reproduce byte-for-byte."""
    out = group.compute(ctx)
    return out.filter(pl.col("minute") == out["minute"].max())


def test_cross_sectional_rank_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=3)
    ctx = BatchContext(frames={"minute_agg": stream})
    group = CrossSectionalRankGroup()
    _assert_equal(
        _whole_buffer_latest(group, ctx),
        group.compute_latest(ctx),
        "cross_sectional_rank",
    )
    # the bounded window must cover the deepest return lag.
    assert max(RETURN_WINDOWS) + 1 >= max(RETURN_WINDOWS)


def test_return_dispersion_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=5)
    daily = (
        pl.DataFrame(
            {
                "symbol": [f"S{i:03d}" for i in range(40) for _ in range(10)],
                "date": [BASE.date() - dt.timedelta(days=9 - d) for _ in range(40) for d in range(10)],
                "close": [100.0 + i + d for i in range(40) for d in range(10)],
            }
        )
        .with_columns(pl.col("date").cast(pl.Date))
        .sort(["symbol", "date"])
    )
    ctx = BatchContext(frames={"minute_agg": stream, "daily": daily})
    group = ReturnDispersionGroup()
    _assert_equal(
        _whole_buffer_latest(group, ctx),
        group.compute_latest(ctx),
        "return_dispersion",
    )
    assert max(MINUTE_WINDOWS) + 1 >= max(MINUTE_WINDOWS)


def test_peer_relative_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=7)
    # a behavioral-cluster map (the static nightly lookup): 4 clusters + a few unmapped names (null cluster).
    reference = pl.DataFrame(
        {
            "symbol": [f"S{i:03d}" for i in range(40)],
            "cluster_id": [None if i % 11 == 0 else i % 4 for i in range(40)],
        }
    )
    ctx = BatchContext(frames={"minute_agg": stream, "reference": reference})
    group = PeerRelativeReturnGroup()
    _assert_equal(
        _whole_buffer_latest(group, ctx),
        group.compute_latest(ctx),
        "peer_relative",
    )
    assert max(PEER_WINDOWS) + 1 >= max(PEER_WINDOWS)
