"""Unit tests for peer_relative — a name's return minus its behavioral-cluster mean.

Hand-built minute_agg + reference (cluster_id) frames with a known per-cluster cross-section lock
in the peer-demean. Parity (compute_latest == compute on the last minute) is covered by the generic
tests/test_fp_latest.py.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _ctx() -> BatchContext:
    """5 symbols, 2 clusters. At minute 5 the 5m returns are engineered:
    cluster 0 = {AAA +0.02, BBB 0.00, CCC -0.02} (mean 0) -> peer_rel = the raw return.
    cluster 1 = {DDD +0.03, EEE +0.01} (mean +0.02) -> peer_rel = +0.01 / -0.01.
    UNMAPPED = ZZZ (cluster_id null) -> peer_rel NULL."""
    paths = {
        "AAA": [100, 100, 100, 100, 100, 102.0],
        "BBB": [100, 100, 100, 100, 100, 100.0],
        "CCC": [100, 100, 100, 100, 100, 98.0],
        "DDD": [100, 100, 100, 100, 100, 103.0],
        "EEE": [100, 100, 100, 100, 100, 101.0],
        "ZZZ": [100, 100, 100, 100, 100, 105.0],
    }
    rows = []
    for sym, px in paths.items():
        for i, price in enumerate(px):
            rows.append(
                {
                    "symbol": sym,
                    "minute": BASE + timedelta(minutes=i),
                    "close": float(price),
                }
            )
    minute = pl.DataFrame(rows)
    reference = pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC", "DDD", "EEE"],
            "cluster_id": [0, 0, 0, 1, 1],
        }
    ).cast({"cluster_id": pl.Int32})
    # ZZZ deliberately absent from the reference -> left-join gives null cluster_id.
    return BatchContext(frames={"minute_agg": minute, "reference": reference})


def _row(out: pl.DataFrame, sym: str, minute_idx: int) -> dict:
    return out.filter(
        (pl.col("symbol") == sym)
        & (pl.col("minute") == BASE + timedelta(minutes=minute_idx))
    ).row(0, named=True)


def test_peer_demean_cluster0_zero_mean() -> None:
    out = run_group(REGISTRY.get_group("peer_relative"), _ctx())
    # cluster 0 mean return = 0 -> peer_rel == raw return.
    assert _row(out, "AAA", 5)["peer_relative_ret_5m"] == pytest.approx(0.02)
    assert _row(out, "CCC", 5)["peer_relative_ret_5m"] == pytest.approx(-0.02)


def test_peer_demean_cluster1_nonzero_mean() -> None:
    out = run_group(REGISTRY.get_group("peer_relative"), _ctx())
    # cluster 1 returns {+0.03, +0.01}, mean +0.02 -> peer_rel = +0.01 / -0.01.
    assert _row(out, "DDD", 5)["peer_relative_ret_5m"] == pytest.approx(0.01)
    assert _row(out, "EEE", 5)["peer_relative_ret_5m"] == pytest.approx(-0.01)


def test_unmapped_symbol_is_null() -> None:
    out = run_group(REGISTRY.get_group("peer_relative"), _ctx())
    assert _row(out, "ZZZ", 5)["peer_relative_ret_5m"] is None


def test_demean_sums_to_zero_within_cluster() -> None:
    """Sanity: the peer-relative returns within a cluster sum to ~0 (it's a demean)."""
    out = run_group(REGISTRY.get_group("peer_relative"), _ctx())
    c0 = sum(_row(out, s, 5)["peer_relative_ret_5m"] for s in ("AAA", "BBB", "CCC"))
    assert c0 == pytest.approx(0.0, abs=1e-9)


def _ctx_zero_close() -> BatchContext:
    """A degenerate cluster-0 member (FFF) with a zero close 5 bars back: the 5m ratio close/_lag5
    would otherwise be ±Inf and poison the cluster demean. Both members map to cluster 0."""
    paths = {
        "AAA": [100, 100, 100, 100, 100, 102.0],
        "FFF": [0.0, 100, 100, 100, 100, 101.0],  # zero close at the _lag5 source -> ratio ±Inf without guard
    }
    rows = []
    for sym, px in paths.items():
        for i, price in enumerate(px):
            rows.append(
                {"symbol": sym, "minute": BASE + timedelta(minutes=i), "close": float(price)}
            )
    minute = pl.DataFrame(rows)
    reference = pl.DataFrame({"symbol": ["AAA", "FFF"], "cluster_id": [0, 0]}).cast(
        {"cluster_id": pl.Int32}
    )
    return BatchContext(frames={"minute_agg": minute, "reference": reference})


def test_zero_past_close_is_null_not_inf() -> None:
    """A zero past close (div-by-zero) yields NULL, never ±Inf, identically in compute and
    compute_latest — and so does not Inf-poison the cluster-mean demean of its peers."""
    group = REGISTRY.get_group("peer_relative")
    ctx = _ctx_zero_close()
    out = run_group(group, ctx)
    fff = _row(out, "FFF", 5)["peer_relative_ret_5m"]
    aaa = _row(out, "AAA", 5)["peer_relative_ret_5m"]
    # FFF's own ratio is undefined (div-by-zero) -> NULL, not Inf.
    assert fff is None
    # AAA stays finite (its peer-mean demean is not poisoned by an Inf sibling).
    assert aaa is not None
    assert math.isfinite(aaa)
