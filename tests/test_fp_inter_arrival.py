"""Unit tests for the inter_arrival microstructure group (Layer C, trades frame).

Hand-built tick frames with known gap timing pin the per-cell math. Synthetic parity is covered by the
T+1 real-data harness (the generic test_fp_latest skips trades-frame groups — no trades frame in the
standard test frames), so these tests pin the formulas directly.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _row(out: pl.DataFrame, minute: datetime) -> dict:
    return out.filter(pl.col("minute") == minute).row(0, named=True)


def _trades(offsets_us: list[int]) -> pl.DataFrame:
    """One symbol, trades at BASE + each microsecond offset (all within minute 0 unless an offset crosses)."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(offsets_us),
            "ts": [BASE + timedelta(microseconds=us) for us in offsets_us],
            "price": [100.0] * len(offsets_us),
            "size": [10.0] * len(offsets_us),
        }
    )


def test_rapid_fire_and_p10_gaps() -> None:
    # Trades at t=0, 50ms, 60ms, 1000ms within minute 0. Gaps (within-minute): 50ms, 10ms, 940ms.
    #   rapid_fire (<100ms): 50ms yes, 10ms yes, 940ms no -> 2/3.
    #   p10 of [10, 50, 940] ms (linear interpolation, sorted [10, 50, 940]): rank = 0.1*2 = 0.2 ->
    #     10 + 0.2*(50-10) = 18.0 ms.
    ms = 1000  # microseconds per millisecond
    out = run_group(
        REGISTRY.get_group("inter_arrival"),
        BatchContext(frames={"trades": _trades([0, 50 * ms, 60 * ms, 1000 * ms])}),
    )
    r = _row(out, BASE)
    assert r["rapid_fire_ratio_1m"] == pytest.approx(2.0 / 3.0)
    assert r["p10_inter_arrival_ms_1m"] == pytest.approx(18.0)


def test_timing_entropy_even_vs_clustered() -> None:
    # Even: one trade in each of 3 distinct seconds -> counts [1,1,1] -> entropy ln(3)/ln(3) = 1.0.
    sec = 1_000_000  # microseconds per second
    even = run_group(
        REGISTRY.get_group("inter_arrival"),
        BatchContext(frames={"trades": _trades([0, 1 * sec, 2 * sec])}),
    )
    assert _row(even, BASE)["trade_timing_entropy_1m"] == pytest.approx(1.0)

    # Clustered: counts [3, 1] across 2 active seconds. p=[0.75,0.25].
    #   H = -(0.75 ln0.75 + 0.25 ln0.25) ; normalized by ln(2).
    clustered = run_group(
        REGISTRY.get_group("inter_arrival"),
        BatchContext(frames={"trades": _trades([0, 100, 200, 1 * sec])}),
    )
    expected = -(0.75 * math.log(0.75) + 0.25 * math.log(0.25)) / math.log(2)
    assert _row(clustered, BASE)["trade_timing_entropy_1m"] == pytest.approx(expected)


def test_single_trade_minute_nulls_gaps_zero_entropy() -> None:
    # A single trade: no gap exists -> rapid_fire / p10 null; one active second -> entropy 0.
    out = run_group(REGISTRY.get_group("inter_arrival"), BatchContext(frames={"trades": _trades([0])}))
    r = _row(out, BASE)
    assert r["rapid_fire_ratio_1m"] is None
    assert r["p10_inter_arrival_ms_1m"] is None
    assert r["trade_timing_entropy_1m"] == pytest.approx(0.0)


def test_empty_frame() -> None:
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}
    )
    out = run_group(REGISTRY.get_group("inter_arrival"), BatchContext(frames={"trades": empty}))
    assert out.height == 0
    assert set(out.columns) == {
        "symbol",
        "minute",
        "rapid_fire_ratio_1m",
        "p10_inter_arrival_ms_1m",
        "trade_timing_entropy_1m",
    }
