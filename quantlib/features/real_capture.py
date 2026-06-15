"""Real-Alpaca capture adapter — connects via alpaca-py's StockDataStream and feeds the SHARED
``process_bars`` core (the same compute/store code as the mock; only the connection differs).

Alpaca delivers 1-minute bars one-at-a-time per symbol shortly after each minute closes; we batch by
minute and flush a completed minute to the core when the next minute's bars start arriving.
``STREAM_URL_OVERRIDE`` (env) can point at a protocol-faithful mock; unset = the real feed.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

import polars as pl

from quantlib.features.backfill_bars import backfill_daily
from quantlib.features.capture import DEFAULT_BUFFER_MINUTES, CaptureState, process_bars
from quantlib.features.loaders import load_reference
from quantlib.features.sharded_capture import INDEX_SYMBOLS, process_reduce, route_minute, shard_of, worker_main


def _shard_snapshots(snapshots: dict | None, symbols: list[str], shard_id: int, n_shards: int) -> dict | None:
    """Slice the reference/daily snapshots to just the symbols this shard owns (+ the replicated index
    ETFs) — so a spawned worker is handed only its share, not the whole universe's daily history."""
    if snapshots is None:
        return None
    shard_symbols = [s for s in symbols if shard_of(s, n_shards) == shard_id] + list(INDEX_SYMBOLS)
    keep = set(shard_symbols)
    return {name: frame.filter(pl.col("symbol").is_in(keep)) for name, frame in snapshots.items()}


def _reader_bench_path(root: str) -> Path | None:
    """Reader-side latency log (route+reduce) when FP_BENCH_LOG is set — benchmark/demo only."""
    if not os.environ.get("FP_BENCH_LOG"):
        return None
    path = Path(root) / "_bench" / "reader.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build_stream() -> StockDataStream:
    feed = DataFeed.SIP if os.environ.get("ALPACA_DATA_FEED", "sip").lower() == "sip" else DataFeed.IEX
    return StockDataStream(
        os.environ["ALPACA_KEY_ID"],
        os.environ["ALPACA_SECRET_KEY"],
        feed=feed,
        url_override=os.environ.get("STREAM_URL_OVERRIDE"),
    )


def run_capture(symbols: list[str], root: str, mode: str, window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None) -> None:
    state = CaptureState()
    pending: dict = {"minute": None, "bars": []}
    # Load the slowly-changing snapshots ONCE at startup; held for the session so the sector/asset-flag
    # groups (reference) and the multi-day/prior-day groups (daily history) serve live off the SAME
    # frames the backfill + parity paths use. Daily needs the session date to anchor the prior close.
    snapshots = {"reference": load_reference()}
    if day is not None:
        snapshots["daily"] = backfill_daily(day, symbols)
    stream = build_stream()

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            process_bars(state, pending["bars"], root, mode, day, window, snapshots)
            pending["bars"] = []
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()


def run_sharded_capture(  # pragma: no cover (live multiprocess loop; logic is unit-tested in sharded_capture)
    symbols: list[str], root: str, mode: str, n_shards: int | None = None,
    window: int = DEFAULT_BUFFER_MINUTES, day: str | None = None, max_minutes: int | None = None,
    snapshots: dict | None = None,
) -> None:
    """Production scale-out: ONE reader owns the websocket and routes each completed minute to N
    persistent worker processes by hash(symbol); the reader runs the universe-wide reduce itself. Each
    worker computes the SAME group code on its shard (byte-identical to single-process — proven in
    tests/test_fp_sharding.py), and writes only its own symbols (partition-disjoint, no DB contention).

    ``snapshots`` overrides the slowly-changing reference/daily frames (else they are loaded from the DB /
    Alpaca) — used by the streaming benchmark to run fully standalone with synthetic snapshots."""
    # ~4 cores per shard: fewer, fatter, multi-threaded shards minimize the per-minute critical path (less
    # process-concurrency contention). The optimum moved toward fewer shards as the declarative batching cut
    # per-shard memory traffic. Measured at 10k/32-cores (compute-only p99): 10 shards=661ms, 8=617ms (best),
    # 6=696ms. (Earlier, pre-batching: 30=1929, 16=1720, 10=1551ms.)
    n_shards = n_shards or max(1, (os.cpu_count() or 8) // 4)
    if snapshots is None:
        snapshots = {"reference": load_reference()}
        if day is not None:
            snapshots["daily"] = backfill_daily(day, symbols)
    # Pin each worker's polars to a slice of the cores. Without this every one of the N spawned workers
    # defaults to a full all-core rayon pool, so N workers x C cores = N*C threads thrash C cores (measured:
    # ~20x slower per shard). One thread per shard when n_shards ~= cores; the spawned children inherit this
    # env at import. (The reader keeps its already-initialized default pool for the universe-wide reduce.)
    threads_per_worker = max(1, (os.cpu_count() or n_shards) // n_shards)
    os.environ["POLARS_MAX_THREADS"] = str(threads_per_worker)

    # SPAWN, not fork: the reader process has already initialized polars' rayon threadpool (building the
    # reference/daily snapshots), and forking a process with a live threadpool deadlocks the child on its
    # first parallel polars op. Spawned workers get a fresh interpreter + fresh threadpool — no inheritance.
    ctx = mp.get_context("spawn")
    queues = [ctx.Queue() for _ in range(n_shards)]
    workers = [
        ctx.Process(
            target=worker_main,
            args=(i, n_shards, queues[i], root, mode, window, day, _shard_snapshots(snapshots, symbols, i, n_shards)),
            daemon=True,
        )
        for i in range(n_shards)
    ]
    for worker in workers:
        worker.start()

    reduce_state = CaptureState()
    pending: dict = {"minute": None, "bars": [], "done": 0, "arrival": 0.0}
    stream = build_stream()

    bench_reader = _reader_bench_path(root)  # FP_BENCH_LOG: time the single-threaded reader (route+reduce)

    def dispatch(bars: list[dict], arrival_wallclock: float) -> None:
        start = time.perf_counter()
        for shard_id, shard_bars in enumerate(route_minute(bars, n_shards)):
            if shard_bars:
                # (arrival_wallclock, bars) — the worker records bar->vector latency off this wall-clock
                # stamp (time.time(), comparable across the reader/worker processes; perf_counter is not).
                queues[shard_id].put((arrival_wallclock, shard_bars))  # map: per-shard workers
        process_reduce(reduce_state, bars, root, mode, day, window)  # gather: universe-wide rank
        if bench_reader is not None:
            with bench_reader.open("a") as handle:
                handle.write(json.dumps({"minute": max(bar["t"] for bar in bars),
                                         "ms": (time.perf_counter() - start) * 1000.0}) + "\n")

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            dispatch(pending["bars"], pending["arrival"])
            pending["bars"] = []
            pending["done"] += 1
            if max_minutes is not None and pending["done"] >= max_minutes:
                if os.environ.get("FP_BENCH_LOG"):
                    print(f"[reader] {pending['done']} minutes dispatched; sending sentinels + stopping",
                          file=sys.stderr, flush=True)
                for queue in queues:
                    queue.put(None)  # shutdown sentinel: workers drain their queue then exit
                await stream.stop_ws()
                return
        if not pending["bars"]:
            # Wall-clock the instant THIS minute's first bar landed off the websocket — the honest "bar
            # arrival" anchor for the bar->vector latency metric (recorded by the worker after assemble).
            pending["arrival"] = time.time()
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    stream.subscribe_bars(on_bar, *symbols)
    stream.run()
    if os.environ.get("FP_BENCH_LOG"):
        print("[reader] stream.run() returned; joining workers", file=sys.stderr, flush=True)
    for worker in workers:
        worker.join(timeout=300)  # bounded mode: let workers drain their queued minutes before exit


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.real_capture <sym,sym> <root> <real|mock> [day] [--sharded]")
    symbols, root, mode = sys.argv[1].split(","), sys.argv[2], sys.argv[3]
    day = sys.argv[4] if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else None
    if "--sharded" in sys.argv:
        run_sharded_capture(symbols, root, mode, day=day)
    else:
        run_capture(symbols, root, mode, day=day)


if __name__ == "__main__":
    main()
