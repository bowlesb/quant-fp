"""Sharded live feature capture — split the per-minute compute across many processes (FP2 scale-out).

The single-process path (``capture.process_bars``) computes every symbol in one polars call; measured,
that does not fit the minute at 10k tickers. This runs the IDENTICAL ``process_bars`` core across N
worker processes partitioned by ``hash(symbol) % N`` (the Edgar/ingestor model), recovering near-linear
parallelism. Each worker owns a disjoint symbol set, holds its own trailing buffer, and writes only its
own symbols (partition-disjoint store writes → no contention). The SAME group code runs per shard, so
per-symbol features are byte-identical to single-process — parity preserved.

Two cross-symbol concerns:
- **Index context** (market_context / market_beta need SPY/QQQ): the index symbols are REPLICATED into
  every shard's bar batch, so each shard has them locally and those groups compute correctly per shard.
- **Universe-wide reduce** (cross_sectional_rank needs ALL symbols): those groups are EXCLUDED from the
  shards and run once in a gather phase over a minimal full-universe (close+volume) buffer held by the
  reader.

The reader owns the single Alpaca websocket (one per account), batches a completed minute, routes it to
the worker queues, and runs the reduce. Workers are persistent (warmup paid once).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from quantlib.features import metrics
from quantlib.features.capture import CaptureState, StoreWriter, process_bars

# Index ETFs replicated to every shard so market-context/beta compute locally (tiny, ~3 symbols).
INDEX_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
# Each shard worker exposes /metrics here (Prometheus scrapes BASE + shard_id) — keep in sync with the
# feature-capture job in config/prometheus/prometheus.yml.
WORKER_METRICS_BASE_PORT = 9201
# Groups that depend on the WHOLE universe at a minute — run in the gather phase, not per shard.
REDUCE_GROUPS: tuple[str, ...] = ("cross_sectional_rank",)


def _bench_log_path(root: str, shard_id: int) -> Path | None:
    """Per-shard latency log path when FP_BENCH_LOG is set (benchmark/demo only; off in production)."""
    if not os.environ.get("FP_BENCH_LOG"):
        return None
    path = Path(root) / "_bench" / f"shard-{shard_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def shard_of(symbol: str, n_shards: int) -> int:
    """Stable shard assignment, identical across processes (Python's hash() is per-process salted)."""
    return int(hashlib.md5(symbol.encode()).hexdigest(), 16) % n_shards


def route_minute(bars: list[dict], n_shards: int) -> list[list[dict]]:
    """Partition a minute's bars by ``hash(symbol) % n_shards``, replicating the index symbols into
    every shard so the market-context groups have their reference series locally."""
    index_bars = [bar for bar in bars if bar["S"] in INDEX_SYMBOLS]
    routed: list[list[dict]] = [list(index_bars) for _ in range(n_shards)]
    for bar in bars:
        if bar["S"] in INDEX_SYMBOLS:
            continue  # already replicated to all shards
        routed[shard_of(bar["S"], n_shards)].append(bar)
    return routed


def process_shard(state: CaptureState, bars: list[dict], root: str, mode: str, day: str | None,
                  window: int, snapshots: dict | None = None, write: bool = True, shard: int | None = None,
                  accumulate: bool = False) -> None:
    """One shard's map step: the shared core, minus the universe-wide reduce groups. Each minute appends
    its OWN per-minute file inside the partition (atomic, no clobber) so all shards write concurrently."""
    process_bars(state, bars, root, mode, day, window, snapshots, exclude_groups=REDUCE_GROUPS,
                 write=write, shard=shard, accumulate=accumulate)


def process_reduce(reduce_state: CaptureState, bars: list[dict], root: str, mode: str, day: str | None,
                   window: int, write: bool = True, accumulate: bool = False) -> None:
    """The gather step: compute the universe-wide reduce groups over ALL symbols once. The reader holds
    a minimal full-universe buffer (close+volume only) and runs ONLY the reduce groups on it."""
    process_bars(reduce_state, bars, root, mode, day, window, only_groups=REDUCE_GROUPS,
                 write=write, accumulate=accumulate)


def worker_main(shard_id: int, n_shards: int, queue, root: str, mode: str, window: int,
                day: str | None, snapshots: dict | None) -> None:  # pragma: no cover (process entry)
    """Persistent worker process entry: own ``shard_id``, drain the queue of minute bar-batches (already
    routed to this shard), and run the map step. A ``None`` batch is the shutdown sentinel."""
    state = CaptureState()
    if os.environ.get("FP_ASYNC_WRITE"):  # opt-in: a background writer thread (can contend with compute)
        state.writer = StoreWriter()
    metrics.start_metrics_server(WORKER_METRICS_BASE_PORT + shard_id)  # /metrics for Prometheus/Grafana
    bench_log = _bench_log_path(root, shard_id)  # set FP_BENCH_LOG=1 to record per-minute shard latency
    if bench_log is not None:
        print(f"[worker {shard_id}] up", file=sys.stderr, flush=True)
    processed = 0
    while True:
        batch = queue.get()
        if batch is None:
            if state.writer is not None:
                state.writer.flush()  # drain pending writes before exit so nothing is lost
                state.writer.stop()
            if bench_log is not None:
                print(f"[worker {shard_id}] exiting after {processed} minutes", file=sys.stderr, flush=True)
            return
        processed += 1
        start = time.perf_counter()
        process_shard(state, batch, root, mode, day, window, snapshots, shard=shard_id)
        if bench_log is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            record = {"shard": shard_id, "minute": max(bar["t"] for bar in batch),
                      "ms": elapsed_ms, "groups": dict(state.group_timings)}
            with bench_log.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
