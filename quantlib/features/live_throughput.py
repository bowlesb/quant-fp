"""Honest full-universe throughput on the LIVE ``process_bars`` path — the number a per-minute bet actually
pays at scale.

WHY NOT ``profile_sim`` / ``stream_sim``: the streaming sim runs the BOUNDED consolidated emit
(``emit_point_in_time`` / ``emit_daily_broadcast`` + the reduction ``emit_numpy`` fast path), which is a
DIFFERENT, more-optimized code path than the live capture core. It UNDER-COUNTS the real per-minute cost
(measured ~2x on the live path) — and at the time of writing it is also broken on ``main`` (the consolidated
daily path references a method the DailySnapshotGroup migration removed). So for an honest full-universe
figure, time the REAL ``process_bars`` directly.

WHAT THIS MEASURES: per shard, warm a ``CaptureState`` ring to ``window`` minutes of bars for the shard's
symbols, then time ``process_bars(write=False)`` over a steady-state minute — the exact compute the live fc
runs each tick (every group's ``compute_latest`` / incremental fold), MINUS the store write/IPC/bus (those are
off the critical path). The live topology is ``n_shards = cpu_count // 4`` parallel processes (4 polars
threads each), and the per-minute UNIVERSE latency is the SLOWEST shard (they run in parallel) — so one
shard's steady-state ``process_bars`` ms at the per-shard symbol count IS the per-minute bar->vector wall.

Run ONE shard (the per-shard cost; multiply nothing — 8 of these run in parallel live):

    docker run --rm --cpuset-cpus=0-3 -e POLARS_MAX_THREADS=4 \
        -e ALPACA_KEY_ID=mock -e ALPACA_SECRET_KEY=mock \
        -e DB_PASSWORD=mock -e DB_HOST=localhost -e DB_PORT=5432 -e DB_NAME=mock -e DB_USER=mock \
        -v "$PWD":/app -w /app fp-dev \
        python -m quantlib.features.live_throughput <syms_per_shard> [warmup_min] [measure_min] [window]

To capture REAL 8-way contention (the honest figure), launch ``n_shards`` of these simultaneously, each
pinned to its own 4-core set across the box, and take the SLOWEST shard's p50/p99. Pin to IDLE cores
(load-avg is misleading on the shared prod box — check ``mpstat -P ALL`` for true per-core idle).
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
import sys

from quantlib.features.bench_stream import SESSION_DAY, synth_daily, synth_reference, synth_symbols
from quantlib.features.capture import DEFAULT_BUFFER_MINUTES, CaptureState, process_bars

BASE = dt.datetime(2026, 6, 16, 13, 30, tzinfo=dt.timezone.utc)


def _bars_for_minute(symbols: list[str], minute_index: int) -> list[dict]:
    """One minute's normalized bar dicts (the ``process_bars`` input shape: S/o/c/h/l/v/t with ``t`` an ISO
    string, matching both the mock JSON feed and the normalized live Alpaca bars)."""
    timestamp = (BASE + dt.timedelta(minutes=minute_index)).isoformat()
    bars: list[dict] = []
    for symbol_index, symbol in enumerate(symbols):
        base_price = 100.0 + (symbol_index % 97) * 0.1 + minute_index * 0.01
        bars.append(
            {
                "S": symbol,
                "o": base_price,
                "c": base_price + 0.05,
                "h": base_price + 0.1,
                "l": base_price - 0.1,
                "v": 1000.0 + symbol_index + minute_index,
                "t": timestamp,
            }
        )
    return bars


def _percentile(sorted_ms: list[float], pct: float) -> float:
    rank = (len(sorted_ms) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return sorted_ms[int(rank)]
    return sorted_ms[low] * (high - rank) + sorted_ms[high] * (rank - low)


def measure_shard(
    syms_per_shard: int, warmup_minutes: int, measure_minutes: int, window: int
) -> list[float]:
    """Warm one shard's ``CaptureState`` ring to ``warmup_minutes`` then return the per-minute
    ``process_bars`` ms over ``measure_minutes`` steady-state minutes (compute only, ``write=False``)."""
    symbols = synth_symbols(syms_per_shard)
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}
    state = CaptureState()
    day = str(BASE.date())
    for minute_index in range(warmup_minutes):
        process_bars(
            state,
            _bars_for_minute(symbols, minute_index),
            root="/tmp/live_throughput_store",
            mode="real",
            day=day,
            window=window,
            snapshots=snapshots,
            write=False,
        )
    times_ms: list[float] = []
    import time

    for minute_index in range(warmup_minutes, warmup_minutes + measure_minutes):
        start = time.perf_counter()
        process_bars(
            state,
            _bars_for_minute(symbols, minute_index),
            root="/tmp/live_throughput_store",
            mode="real",
            day=day,
            window=window,
            snapshots=snapshots,
            write=False,
        )
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return sorted(times_ms)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m quantlib.features.live_throughput <syms_per_shard> [warmup_min] [measure_min] "
            "[window]"
        )
    syms_per_shard = int(sys.argv[1])
    warmup = int(sys.argv[2]) if len(sys.argv) > 2 else 245
    measure = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    window = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_BUFFER_MINUTES

    print(f"warming {syms_per_shard} syms x {warmup}m buffer (window {window}) ...", flush=True)
    times = measure_shard(syms_per_shard, warmup, measure, window)
    print(
        f"\n=== LIVE process_bars, {syms_per_shard} syms/shard (1 of cpu//4 parallel shards), "
        f"warmed {window}m, compute-only ===\n"
        f"per-minute (one shard) p50={statistics.median(times):.0f}ms  p95={_percentile(times, 95):.0f}ms  "
        f"p99={_percentile(times, 99):.0f}ms  max={max(times):.0f}ms\n"
        f"=> universe per-minute (n_shards in parallel) ~ the SLOWEST shard's number; "
        f"do NOT multiply by symbols (the engine is frame-vectorized across symbols within a shard)."
    )


if __name__ == "__main__":
    main()
