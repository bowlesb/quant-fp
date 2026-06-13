"""Sub-minute (Layer-C) soundness tests — the hard case.

The core claim the whole sub-minute thesis rests on: a feature defined as a pure function of the
ticks SORTED BY EXCHANGE TIMESTAMP gives the same value whether the ticks arrived live (buffered in
arrival order, then computed at the minute boundary) or were fetched in tape order via backfill —
because both sort by ts and compute the identical function. These tests prove that, including a
genuinely PATH-DEPENDENT feature (max_runup_1m), and document the one residual risk (same-timestamp
ticks), which is the thing to watch as more order-sensitive features are added.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.engine import run_group
from quantlib.features.registry import REGISTRY

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _ticks_distinct_ts(n: int = 120) -> list[dict]:
    # distinct sub-second timestamps; a wiggly price path so run-up is genuinely order-dependent
    return [
        {"symbol": "AAA", "ts": BASE + timedelta(seconds=i % 60, microseconds=(i * 137) % 1_000_000),
         "price": 100.0 + math.sin(i / 3.0), "size": 10.0 + (i % 5)}
        for i in range(n)
    ]


def _burst(ticks: list[dict]) -> pl.DataFrame:
    group = REGISTRY.get_group("microstructure_burst")
    return run_group(group, BatchContext(frames={"trades": pl.DataFrame(ticks)})).sort(["symbol", "minute"])


def test_subminute_features_are_arrival_order_invariant() -> None:
    """LIVE (ticks buffered in arrival order) == BACKFILL (tape order) for the SAME tick set."""
    ticks = _ticks_distinct_ts()
    tape_order = _burst(ticks)
    live_order = _burst(list(reversed(ticks)))  # a different arrival order
    assert tape_order.equals(live_order)  # identical -> sorting by ts makes arrival order irrelevant

    import random  # a third, fully shuffled arrival order

    shuffled = ticks[:]
    random.Random(7).shuffle(shuffled)
    assert _burst(shuffled).equals(tape_order)


def test_path_dependent_feature_is_actually_path_dependent() -> None:
    """Guard against a false-positive above: max_runup_1m must genuinely depend on the price path
    (so the invariance test is meaningful, not trivially true)."""
    ticks = _ticks_distinct_ts()
    runup = _burst(ticks).row(0, named=True)["max_runup_1m"]
    # a monotonically rising path has a large run-up; a falling path has ~0
    rising = [{"symbol": "AAA", "ts": BASE + timedelta(seconds=i), "price": 100.0 + i, "size": 1.0} for i in range(10)]
    falling = [{"symbol": "AAA", "ts": BASE + timedelta(seconds=i), "price": 100.0 - i, "size": 1.0} for i in range(10)]
    assert _burst(rising).row(0, named=True)["max_runup_1m"] > 5.0
    assert _burst(falling).row(0, named=True)["max_runup_1m"] == 0.0
    assert runup > 0.0
