"""Parity gate: the bounded ``compute_latest`` rewrites for the cross-sectional GATHER groups.

The minimum-compute sweep (docs/FEATURE_PREP_OVERHAUL.md) found three cross-sectional groups whose
``compute_latest`` lagged / sorted / reduced the WHOLE minute buffer and then kept only the latest minute —
pure waste, since the per-minute cross-sectional reduce (rank / dispersion / peer-demean) at T needs only the
trailing window that feeds T's returns. Each was rewritten to slice the minute buffer to its deepest window
first (``compute_latest_on_window`` / an explicit minute-only slice), removing the whole-buffer pass.

This MUST be value-identical. The generic ``test_fp_latest`` proves ``compute_latest == compute().filter(T)``
for every group, but it SKIPS these three because the standard synthetic frames carry no ``reference`` cluster
map / ``universe`` / matching ``daily`` — so this module supplies those frames (with a deliberately SPARSE
symbol, the case the bounded slice must get right) and pins cell-for-cell equality.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl


from quantlib.features import BatchContext, REGISTRY

_BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
_SYMBOLS = ["A", "B", "C", "D", "E", "F"]  # F is the sparse symbol (skips minutes)


def _minute_agg() -> pl.DataFrame:
    rows = []
    for i in range(90):  # > the deepest 60m window, warm
        for sym in _SYMBOLS:
            if sym == "F" and i % 4 == 0:  # sparse: F is absent every 4th minute
                continue
            rows.append(
                {
                    "symbol": sym,
                    "minute": _BASE + timedelta(minutes=i),
                    "close": 100.0 + i * 0.13 + (hash(sym) % 5),
                    "volume": 1000.0 + i + (hash(sym) % 9) * 10,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _daily() -> pl.DataFrame:
    rows = []
    for day in range(12):
        for sym in _SYMBOLS:
            rows.append({"symbol": sym, "date": date(2026, 6, 1) + timedelta(days=day), "close": 100.0 + day + (hash(sym) % 5)})
    return pl.DataFrame(rows)


def _assert_latest_matches_rolling(group_name: str, frames: dict[str, pl.DataFrame]) -> None:
    group = REGISTRY.get_group(group_name)
    ctx = BatchContext(frames=frames)
    rolling = group.compute(ctx)
    latest = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == latest).sort("symbol")
    actual = group.compute_latest(ctx).sort("symbol").select(expected.columns)
    assert actual.height == expected.height
    features = [c for c in expected.columns if c not in ("symbol", "minute")]
    assert features, "no features to compare"
    for feature in features:
        joined = expected.select("symbol", feature).join(
            actual.select("symbol", pl.col(feature).alias("_a")), on="symbol"
        )
        # bad iff exactly one side is null (a null/value flip) OR both non-null and beyond a tight float tol.
        bad = joined.filter(
            (pl.col(feature).is_null() != pl.col("_a").is_null())
            | ((pl.col(feature) - pl.col("_a")).abs() > 1e-10)
        )
        assert bad.height == 0, f"{group_name}.{feature} diverges:\n{bad}"


def test_peer_relative_bounded_latest_matches_rolling() -> None:
    reference = pl.DataFrame({"symbol": _SYMBOLS, "cluster_id": [0, 0, 1, 1, 1, None]})
    _assert_latest_matches_rolling("peer_relative", {"minute_agg": _minute_agg(), "reference": reference})


def test_cross_sectional_rank_bounded_latest_matches_rolling_with_universe() -> None:
    universe = pl.DataFrame({"symbol": _SYMBOLS})
    _assert_latest_matches_rolling("cross_sectional_rank", {"minute_agg": _minute_agg(), "universe": universe})


def test_cross_sectional_rank_bounded_latest_matches_rolling_no_universe() -> None:
    # The no-pin path (no universe frame) must also stay parity-true under the bounded slice.
    _assert_latest_matches_rolling("cross_sectional_rank", {"minute_agg": _minute_agg()})


def test_return_dispersion_bounded_latest_matches_rolling() -> None:
    universe = pl.DataFrame({"symbol": _SYMBOLS})
    _assert_latest_matches_rolling(
        "return_dispersion", {"minute_agg": _minute_agg(), "daily": _daily(), "universe": universe}
    )
