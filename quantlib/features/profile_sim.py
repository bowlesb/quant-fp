"""Pre-flight latency + per-group profiler — fake-run production through the streaming sim and report the
two things a person needs before the open:

  1. END-TO-END bar->vector latency (p50/p95/p99). For each minute, the universe-wide feature vector for
     that minute is not READY until the SLOWEST shard has assembled its slice, so the bet-relevant latency
     is ``max_over_shards(vector-ready wall) - reader-dispatch wall`` per minute (the LAST-bar anchor, the
     same one real_capture's ``feature_assemble_seconds`` uses — it excludes the post-bet write). This is
     the number that must fit the ~100ms budget as bars flow in.

  2. PER-GROUP compute ranking. The phase decomposition in ``stream_sim._report`` buckets ~82 at-T groups
     into a single "rest" number, which hides WHICH group is slow. With ``FP_SIM_GROUP_TIMINGS=1`` each
     non-reduction group's own ``compute_latest`` ms is logged per minute; here we rank groups by their
     p50/p99 so the slowest one is named, not hidden in an aggregate. (Reduction groups share one batched
     marshal, so their per-group share is the reduction-emit phase split evenly — reported for context.)

Runnable before each open as a pre-flight check:

    docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \
        python -m quantlib.features.profile_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]

It REUSES the exact sim machinery (the real StockDataStream against the msgpack mock, the same shard
workers and incremental fast path) — it only flips the profiler's two logging switches on and prints a
profile-oriented report. See docs/PROFILE_SIM.md for how to read the output.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from quantlib.features.bench_stream import (
    PORT,
    SESSION_DAY,
    _start_mock,
    synth_daily,
    synth_reference,
    synth_symbols,
)
from quantlib.features.capture import DEFAULT_BUFFER_MINUTES
from quantlib.features.stream_sim import _percentile, run_streaming_sim

BUDGET_MS = 100.0  # the bar->vector budget (CLAUDE memory: ~100ms hard ceiling as bars flow in)


def _read_shard_records(bench: Path) -> dict[str, list[dict]]:
    """Group every shard's per-minute bench record by minute (the per-shard JSONL the workers wrote)."""
    by_minute: dict[str, list[dict]] = defaultdict(list)
    for shard_file in sorted(bench.glob("shard-*.jsonl")):
        for line in shard_file.read_text().splitlines():
            record = json.loads(line)
            by_minute[record["minute"]].append(record)
    return by_minute


def _read_dispatch_walls(bench: Path) -> dict[str, float]:
    """The reader's ``dispatch_wall`` per minute (the last-bar-in-hand cross-process wall-clock anchor)."""
    reader_file = bench / "reader.jsonl"
    if not reader_file.exists():
        return {}
    walls: dict[str, float] = {}
    for line in reader_file.read_text().splitlines():
        record = json.loads(line)
        if "dispatch_wall" in record:
            walls[record["minute"]] = record["dispatch_wall"]
    return walls


def end_to_end_latencies_ms(by_minute: dict[str, list[dict]], dispatch_walls: dict[str, float],
                            warmup: int) -> list[float]:
    """Per minute, the universe vector is ready when the SLOWEST shard finished: ``max(ready_wall) -
    dispatch_wall``, in ms, over the steady-state (post-warmup) minutes that have both stamps."""
    minutes_sorted = sorted(by_minute)
    latencies: list[float] = []
    for minute in minutes_sorted:
        if minute not in dispatch_walls:
            continue
        readies = [rec["ready_wall"] for rec in by_minute[minute] if "ready_wall" in rec]
        if not readies:
            continue
        latencies.append((max(readies) - dispatch_walls[minute]) * 1000.0)
    return latencies[warmup:]


def rank_groups(by_minute: dict[str, list[dict]], warmup: int) -> list[tuple[str, float, float, float]]:
    """Rank non-reduction groups by p50 of their per-group ``compute_latest`` ms. For each (group, minute)
    we take the SLOWEST shard's time (the critical shard, consistent with the end-to-end view), then the
    p50/p99/max across the post-warmup minutes. Returns (name, p50, p99, max) sorted slowest-first."""
    minutes_sorted = sorted(by_minute)[warmup:]
    per_group: dict[str, list[float]] = defaultdict(list)
    for minute in minutes_sorted:
        slowest: dict[str, float] = {}
        for rec in by_minute[minute]:
            for name, value in rec.get("group_timings", {}).items():
                if name not in slowest or value > slowest[name]:
                    slowest[name] = value
        for name, value in slowest.items():
            per_group[name].append(value)
    ranked: list[tuple[str, float, float, float]] = []
    for name, values in per_group.items():
        ranked.append((name, statistics.median(values), _percentile(values, 99), max(values)))
    ranked.sort(key=lambda row: -row[1])
    return ranked


def _report(root: str, n_symbols: int, n_shards: int, warmup: int) -> None:
    bench = Path(root) / "_bench"
    by_minute = _read_shard_records(bench)
    dispatch_walls = _read_dispatch_walls(bench)

    end_to_end = end_to_end_latencies_ms(by_minute, dispatch_walls, warmup)
    print(f"\n=== PRE-FLIGHT PROFILE: {n_symbols} symbols, {n_shards} shards "
          f"(~{n_symbols // n_shards}/shard), {len(by_minute)} minutes ===\n")
    print("END-TO-END bar-arrival(last bar) -> universe-vector-ready (slowest shard each minute, "
          "write excluded):")
    if end_to_end:
        p50 = statistics.median(end_to_end)
        p95 = _percentile(end_to_end, 95)
        p99 = _percentile(end_to_end, 99)
        verdict = "PASS" if p99 < BUDGET_MS else "FAIL"
        print(f"    p50={p50:8.1f}ms  p95={p95:8.1f}ms  p99={p99:8.1f}ms  max={max(end_to_end):8.1f}ms")
        print(f"    => p99 vs {BUDGET_MS:.0f}ms budget: {verdict} "
              f"({'under' if p99 < BUDGET_MS else f'{p99 / BUDGET_MS:.1f}x over'})")
    else:
        print("    (no end-to-end stamps — was the sim run via this tool with the profiler flags?)")

    print("\nPER-GROUP compute_latest ranking (slowest shard each minute, post-warmup):")
    ranked = rank_groups(by_minute, warmup)
    if not ranked:
        print("    (no per-group timings — FP_SIM_GROUP_TIMINGS was not set)")
    else:
        print(f"    {'group':<28}{'p50':>10}{'p99':>10}{'max':>10}")
        for name, p50, p99, mx in ranked:
            print(f"    {name:<28}{p50:9.2f}ms{p99:9.2f}ms{mx:9.2f}ms")
        print(f"    {'SUM of per-group p50':<28}{sum(row[1] for row in ranked):9.2f}ms")
        top = ", ".join(f"{name} ({p50:.0f}ms)" for name, p50, _, _ in ranked[:3])
        print(f"\n    TOP-3 slowest groups (p50): {top}")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: python -m quantlib.features.profile_sim <n_symbols> <n_shards> <measure_minutes> "
            "[warmup] [window]"
        )
    n_symbols, n_shards, measure = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    warmup = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    window = int(sys.argv[5]) if len(sys.argv) > 5 else DEFAULT_BUFFER_MINUTES
    total_minutes = warmup + measure

    symbols = synth_symbols(n_symbols)
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}
    root = os.environ.get("BENCH_ROOT", "/tmp/profile_sim_store")

    os.environ["FP_BENCH_LOG"] = "1"
    os.environ["FP_SIM_GROUP_TIMINGS"] = "1"  # the per-group attribution this tool exists to give
    os.environ["STREAM_URL_OVERRIDE"] = f"ws://127.0.0.1:{PORT}"
    os.environ.setdefault("ALPACA_KEY_ID", "mock")
    os.environ.setdefault("ALPACA_SECRET_KEY", "mock")
    # Default to a realistic liquid-name tick firehose (the mock's own default), so the tick path
    # (trade_flow / quote_spread / liquidity / tick_runlength) is stressed at a Monday-like rate, not a
    # token 5/min. Override via MOCK_TRADES_PER_MIN / MOCK_QUOTES_PER_MIN.
    os.environ.setdefault("MOCK_TRADES_PER_MIN", "24")
    os.environ.setdefault("MOCK_QUOTES_PER_MIN", "72")
    os.environ["MOCK_MINUTES"] = str(total_minutes + 2)

    print(f"pre-flight profiling {n_symbols} symbols x {total_minutes} minutes "
          f"(warmup {warmup}, window {window}); root={root}", flush=True)
    _start_mock(total_minutes + 2)
    time.sleep(1.5)
    run_streaming_sim(symbols, root, "mock", n_shards=n_shards, window=window, day=SESSION_DAY,
                      max_minutes=total_minutes, snapshots=snapshots)
    _report(root, n_symbols, n_shards, warmup)


if __name__ == "__main__":
    main()
