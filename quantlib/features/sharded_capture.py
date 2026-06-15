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
from functools import lru_cache
from pathlib import Path

from quantlib.features import latency_drilldown, metrics
from quantlib.features.capture import (
    CaptureState,
    StoreWriter,
    process_bars,
    warm_start_enabled,
    warm_start_ring,
)
from quantlib.features.registry import REGISTRY

# Index ETFs replicated to every shard so market-context/beta compute locally (tiny, ~3 symbols).
INDEX_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
# Each shard worker exposes /metrics here (Prometheus scrapes BASE + shard_id) — keep in sync with the
# feature-capture job in config/prometheus/prometheus.yml.
WORKER_METRICS_BASE_PORT = 9201
# Groups that depend on the WHOLE universe at a minute — run in the gather phase, not per shard.
REDUCE_GROUPS: tuple[str, ...] = ("cross_sectional_rank",)
# Slack minutes on top of the reduce groups' deepest declared window — leaves the leading-edge lookback
# the reduce path needs (e.g. the bar exactly ``window`` ago) defined, exactly as the full buffer did.
REDUCE_WINDOW_SLACK = 30


def reduce_buffer_columns() -> tuple[str, ...]:
    """The bar columns the reduce groups ACTUALLY read — the union of their ``minute_agg`` InputSpec
    columns (cross_sectional_rank: symbol/minute/close/volume). Projecting the reader's reduce buffer to
    just these (instead of the full 7-column frame) is parity-neutral: the dropped columns are never read."""
    columns: list[str] = []
    for name in REDUCE_GROUPS:
        group = REGISTRY.get_group(name)
        for spec in group.inputs:
            if spec.name == "minute_agg":
                for column in spec.columns:
                    if column not in columns:
                        columns.append(column)
    return tuple(columns)


def reduce_buffer_minutes(full_window: int) -> int:
    """The trailing depth the reduce groups need — the max DECLARED window across the reduce groups plus
    ``REDUCE_WINDOW_SLACK``, capped at ``full_window``. Derived from the groups (NOT hardcoded); falls back
    to the full window for any reduce group that doesn't declare its depth (``reduce_buffer_minutes`` None)."""
    declared: list[int] = []
    for name in REDUCE_GROUPS:
        minutes = REGISTRY.get_group(name).reduce_buffer_minutes()
        if minutes is None:
            return full_window  # unknown depth -> keep the full buffer, safe
        declared.append(minutes)
    if not declared:
        return full_window
    return min(full_window, max(declared) + REDUCE_WINDOW_SLACK)


def _bench_log_path(root: str, shard_id: int) -> Path | None:
    """Per-shard latency log path when FP_BENCH_LOG is set (benchmark/demo only; off in production)."""
    if not os.environ.get("FP_BENCH_LOG"):
        return None
    path = Path(root) / "_bench" / f"shard-{shard_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=None)
def shard_of(symbol: str, n_shards: int) -> int:
    """Stable shard assignment, identical across processes (Python's hash() is per-process salted). Cached:
    the symbol set is fixed, so after the first minute this is a dict lookup, not an md5 per bar per minute."""
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
    """The gather step: compute the universe-wide reduce groups over ALL symbols once. The reader holds a
    MINIMAL full-universe buffer — projected to the columns the reduce groups read (close+volume + keys)
    and capped at the reduce groups' deepest declared window + slack, NOT the full 300m — and runs ONLY
    the reduce groups on it. Both the projection and the depth cap are derived from the reduce groups'
    declarations (``reduce_buffer_columns``/``reduce_buffer_minutes``) and are parity-neutral: the dropped
    columns and older minutes were never read on this path."""
    process_bars(reduce_state, bars, root, mode, day, window, only_groups=REDUCE_GROUPS,
                 write=write, accumulate=accumulate,
                 project_columns=reduce_buffer_columns(), buffer_minutes=reduce_buffer_minutes(window))


def worker_main(shard_id: int, n_shards: int, queue, root: str, mode: str, window: int,
                day: str | None, snapshots: dict | None,
                symbols: list[str] | None = None) -> None:  # pragma: no cover (process entry)
    """Persistent worker process entry: own ``shard_id``, drain the queue of minute bar-batches (already
    routed to this shard), and run the map step. A ``None`` batch is the shutdown sentinel.

    ``symbols`` are this shard's owned tickers (+ the replicated index ETFs); when ``FP_WARM_START=1`` and a
    session ``day`` is set, the shard's trailing ring is rehydrated from those symbols' already-settled bars
    BEFORE the first live minute, so a restart does not start cold (CRITICAL-2). Default OFF / no symbols =
    today's cold start, unchanged."""
    state = CaptureState()
    if os.environ.get("FP_ASYNC_WRITE"):  # opt-in: a background writer thread (can contend with compute)
        state.writer = StoreWriter()
    if warm_start_enabled() and day and symbols:
        from quantlib.features.backfill_bars import backfill_bars

        # Alpaca historical RAW = the same unadjusted SIP bars this shard will stream — a parity-true seed.
        bars = backfill_bars(day, symbols)
        seeded = warm_start_ring(state, bars, depth=window)
        print(f"[worker {shard_id}] warm-started ring: {seeded} minutes from {bars.height} bars",
              file=sys.stderr, flush=True)
    metrics.start_metrics_server(WORKER_METRICS_BASE_PORT + shard_id)  # /metrics for Prometheus/Grafana
    bench_log = _bench_log_path(root, shard_id)  # set FP_BENCH_LOG=1 to record per-minute shard latency
    if bench_log is not None:
        print(f"[worker {shard_id}] up", file=sys.stderr, flush=True)
    processed = 0
    while True:
        item = queue.get()
        if item is None:
            if state.writer is not None:
                state.writer.flush()  # drain pending writes before exit so nothing is lost
                state.writer.stop()
            if bench_log is not None:
                print(f"[worker {shard_id}] exiting after {processed} minutes", file=sys.stderr, flush=True)
            return
        # Reader hands (first_arrival, last_arrival, symbol_arrivals, minute, bars). All arrival stamps are
        # time.time() (cross-process comparable; perf_counter is not): first_arrival = the minute's FIRST
        # bar landing (end-to-end anchor, incl. Alpaca delivery spread), last_arrival = the minute's LAST
        # bar landing (pure-compute anchor), symbol_arrivals = per-symbol arrival for the drill-down.
        first_arrival, last_arrival, symbol_arrivals, minute, batch = item
        processed += 1
        start = time.perf_counter()
        process_shard(state, batch, root, mode, day, window, snapshots, shard=shard_id)
        ready_wallclock = time.time()
        # Two complementary latencies, both ending at the assemble (the bet point) with the post-bet
        # parquet write subtracted (process_bars records last_write_ms separately; subtract it whether the
        # write was sync here or async-queued). feature_vector_latency_seconds (first-bar) is end-to-end
        # incl. Alpaca's delivery spread; feature_assemble_seconds (last-bar) is OUR pure compute.
        write_seconds = state.last_write_ms / 1000.0
        metrics.record_bar_to_vector(shard_id, max(0.0, ready_wallclock - first_arrival - write_seconds))
        metrics.record_assemble(shard_id, max(0.0, ready_wallclock - last_arrival - write_seconds))
        # Drill-down (best-effort, fault-isolated, off the hot path): top-K slowest symbols this minute ->
        # latency_slow_symbols. A DB error logs a warning and continues — it must never stall capture.
        minute_boundary_epoch = minute.timestamp()
        slow_rows = latency_drilldown.top_k_slow_symbols(symbol_arrivals, ready_wallclock, minute_boundary_epoch)
        latency_drilldown.write_slow_symbols(minute, shard_id, slow_rows)
        if bench_log is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            # The bet-relevant latency is COMPUTE only; the write happens after the decision, so report it
            # separately and subtract it from the critical-path "ms".
            record = {"shard": shard_id, "minute": max(bar["t"] for bar in batch),
                      "ms": elapsed_ms - state.last_write_ms, "write_ms": state.last_write_ms,
                      "groups": dict(state.group_timings)}
            with bench_log.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
