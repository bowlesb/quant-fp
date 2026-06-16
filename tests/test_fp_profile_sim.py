"""The pre-flight profiler's aggregation is correct — the two pure functions that turn the sim's per-shard
bench records into (a) the end-to-end bar->vector latency and (b) the per-group ranking. These are PURE
(no I/O, no subprocess), so they are unit-tested directly; the full multiprocess sim run is exercised by
the Makefile target (fp-profile-sim), not in CI.
"""
from __future__ import annotations

from quantlib.features.profile_sim import end_to_end_latencies_ms, rank_groups


def test_end_to_end_latency_is_slowest_shard_minus_dispatch() -> None:
    """Per minute the universe vector is ready only when the SLOWEST shard finished, so the latency is
    ``max_over_shards(ready_wall) - dispatch_wall`` (in ms), and the warmup minutes are dropped."""
    by_minute = {
        "m0": [{"ready_wall": 100.10}, {"ready_wall": 100.25}],  # slowest = 100.25
        "m1": [{"ready_wall": 200.05}, {"ready_wall": 200.40}],  # slowest = 200.40
        "m2": [{"ready_wall": 300.30}, {"ready_wall": 300.12}],  # slowest = 300.30
    }
    dispatch_walls = {"m0": 100.00, "m1": 200.00, "m2": 300.00}
    # warmup=1 drops m0; remaining are (200.40-200.00)=400ms and (300.30-300.00)=300ms.
    latencies = end_to_end_latencies_ms(by_minute, dispatch_walls, warmup=1)
    assert len(latencies) == 2
    assert abs(latencies[0] - 400.0) < 1e-6
    assert abs(latencies[1] - 300.0) < 1e-6


def test_end_to_end_skips_minutes_without_both_stamps() -> None:
    """A minute the reader never stamped (no dispatch_wall) or a shard record lacking ready_wall must not
    crash or fabricate a latency — those minutes are simply skipped."""
    by_minute = {
        "m0": [{"ready_wall": 100.20}],
        "m1": [{"ms": 5.0}],  # no ready_wall
    }
    dispatch_walls = {"m0": 100.00}  # m1 absent
    latencies = end_to_end_latencies_ms(by_minute, dispatch_walls, warmup=0)
    assert len(latencies) == 1
    assert abs(latencies[0] - 200.0) < 1e-6


def test_rank_groups_orders_by_p50_taking_slowest_shard() -> None:
    """Per (group, minute) the critical shard's time is taken (the slowest), then groups are ranked by the
    p50 across post-warmup minutes — the slowest group first, named, not hidden in an aggregate."""
    by_minute = {
        "m0": [{"group_timings": {"slow": 999.0, "fast": 999.0}}],  # sorts first -> dropped by warmup
        "m1": [
            {"group_timings": {"slow": 40.0, "fast": 2.0}},
            {"group_timings": {"slow": 50.0, "fast": 3.0}},  # slowest shard for "slow" = 50, "fast" = 3
        ],
        "m2": [
            {"group_timings": {"slow": 60.0, "fast": 1.0}},
            {"group_timings": {"slow": 55.0, "fast": 4.0}},  # slowest: slow=60, fast=4
        ],
    }
    ranked = rank_groups(by_minute, warmup=1)
    names = [row[0] for row in ranked]
    assert names == ["slow", "fast"]  # slow ranked first by p50
    slow_row = ranked[0]
    # p50 over the two post-warmup minutes' slowest-shard values: median(50, 60) = 55
    assert abs(slow_row[1] - 55.0) < 1e-6


def test_rank_groups_empty_when_no_timings() -> None:
    """No group_timings (profiler flag off) -> empty ranking, not a crash."""
    by_minute = {"m0": [{"ms": 5.0}], "m1": [{"ms": 6.0}]}
    assert rank_groups(by_minute, warmup=0) == []
