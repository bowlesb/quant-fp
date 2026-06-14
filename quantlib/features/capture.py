"""Live capture client — buffers per-minute bars from a websocket stream, computes the bar features
over a trailing window, and writes ``source=stream`` to the store.

SAME code for mock and real: the URL selects the feed (a local mock vs the real data websocket), and
the store root's mode marker ('mock'|'real') guarantees simulated data never lands in the real
store. This is the FP2 live path and the Monday capture service.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

import polars as pl
import websockets

from quantlib.features import metrics, store
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch

BARS_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
}

# The trailing buffer MUST exceed the largest declared feature window plus the deepest intra-feature
# lag. Below that, the buffer's leading-edge minutes lack the lookback/lag context the settled
# backfill has (e.g. their one-minute return is null live but defined in backfill), so the longest-
# window features diverge live-vs-backfill — a parity break. Our longest minute window today is
# price_levels' 240m; 300 leaves headroom. (The separate latest-minute compute mode removes the
# per-minute cost of recomputing this whole buffer at 10k-ticker scale.)
DEFAULT_BUFFER_MINUTES = 300


class StoreWriter:
    """Background parquet-writer thread — keeps disk writes OFF the per-minute compute critical path.

    Each minute's compute submits its group frames here and returns immediately (so the compute→bet
    decision never waits on disk); a single daemon thread drains the queue and calls store.write_group,
    overlapping the writes with the NEXT minute's compute. Writes are still per-minute, atomic, and
    idempotent (minute-keyed files). FIFO order preserves re-delivery semantics. ``flush()`` blocks until
    the queue is drained (used at shutdown); ``stop()`` joins the thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                store.write_group(**task)
            finally:
                self._queue.task_done()

    def submit(self, **kwargs: object) -> None:
        self._queue.put(kwargs)

    def flush(self) -> None:
        self._queue.join()

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=30)


class CaptureState:
    """Rolling buffer + accumulated per-group output. Shared across any connection adapter."""

    def __init__(self) -> None:
        self.buffer: pl.DataFrame | None = None  # trailing-window bars, kept AS a frame (no per-minute round-trip)
        self.accumulated: dict[str, pl.DataFrame] = {}  # one DEDUPED frame per group
        self.minutes = 0
        self.group_timings: dict[str, float] = {}  # last per-group compute ms (first-class live timing)
        self.last_write_ms = 0.0  # time spent WRITING last minute — excluded from the bet-relevant compute latency
        self.writer: StoreWriter | None = None  # set by a live worker -> writes go async, off the compute path


def process_bars(
    state: CaptureState,
    bars: list[dict],
    root: str,
    mode: str,
    day: str | None,
    window: int,
    snapshots: dict[str, pl.DataFrame] | None = None,
    exclude_groups: tuple[str, ...] = (),
    only_groups: tuple[str, ...] | None = None,
    write: bool = True,
    shard: int | None = None,
    accumulate: bool = False,
) -> None:
    """The SHARED compute→store core (connection-agnostic). ``bars`` are normalized dicts with keys
    S, o, c, h, l, v, t — the parity boundary: both the mock JSON feed and the real Alpaca Bar objects
    normalize to this shape, then run through the identical code. Robust to RE-DELIVERED minutes
    (reconnect/replay): de-dups on (symbol, minute) keep-last so no duplicate cells corrupt parity.

    ``snapshots`` are slowly-changing reference frames (e.g. ``reference`` for sector/flags, ``daily``
    for multi-day) loaded once and held by the caller, merged into the compute context each minute so
    those groups self-select and serve live — the same frames the backfill/parity path provides.

    ``exclude_groups`` are computed elsewhere — in the sharded executor the cross-sectional reduce
    groups (universe-wide rank) run in a gather phase, not per shard. ``write=False`` skips the store
    (used by the gather phase). ``accumulate=True`` also keeps each group's outputs concatenated in
    ``state.accumulated`` — the in-memory cross-minute record the sharding parity test inspects; it is
    OFF in production because the durable record is the per-minute store files, not RAM."""
    # Build ONLY the new minute's bars into a frame and concat onto the kept buffer — keeping the buffer
    # as a DataFrame avoids round-tripping the whole trailing window through list[dict] every minute (at
    # 10k symbols x window that round-trip dominated the reader/worker per-minute cost). concat([old,new])
    # + unique(keep="last") preserves re-delivery dedup (the new copy of a minute wins).
    new_frame = pl.DataFrame(
        [
            {"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": float(bar["o"]),
             "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"]), "volume": float(bar["v"])}
            for bar in bars
        ],
        schema=BARS_SCHEMA,
    )
    frame = new_frame if state.buffer is None else pl.concat([state.buffer, new_frame])
    frame = frame.unique(subset=["symbol", "minute"], keep="last")
    recent = sorted(frame["minute"].unique())[-window:]
    frame = frame.filter(pl.col("minute").is_in(recent))
    state.buffer = frame  # bound to the trailing window (de-duped), kept as a frame for the next minute
    latest = frame["minute"].max()
    target_day = day or str(latest.date())
    frames = {"minute_agg": frame, **(snapshots or {})}
    ctx = BatchContext(frames=frames)
    selected = [
        group
        for group in runnable(frames)
        if group.name not in exclude_groups and (only_groups is None or group.name in only_groups)
    ]
    # Declarative reduction groups sharing an input run in ONE batched marshal+kernel pass (one symbol-code
    # + numpy copy for all of them); every other group runs individually. Both yield (group, out, ms).
    outputs: list[tuple] = []
    batchable: dict[str, list] = defaultdict(list)
    for group in selected:
        if isinstance(group, ReductionGroup):
            batchable[group.reduce_input].append(group)
        else:
            group_start = time.perf_counter()
            out = group.compute_latest(ctx)  # live = aggregate-at-T (fast where overridden; parity-guarded)
            outputs.append((group, out, (time.perf_counter() - group_start) * 1000.0))
    for batch_groups in batchable.values():
        batch_start = time.perf_counter()
        batched = compute_reduction_batch(batch_groups, ctx)
        per_ms = (time.perf_counter() - batch_start) * 1000.0 / len(batch_groups)
        for group in batch_groups:
            outputs.append((group, batched[group.name], per_ms))

    write_start = time.perf_counter()
    for group, out, compute_ms in outputs:
        state.group_timings[group.name] = compute_ms
        if accumulate:
            prior = state.accumulated.get(group.name)
            state.accumulated[group.name] = (
                out if prior is None else pl.concat([prior, out]).unique(subset=["symbol", "minute"], keep="last")
            )
        if write:
            # Append THIS minute only (minute-keyed file) — O(1) per tick, idempotent on re-delivery.
            # Off the critical path when a StoreWriter is set (live workers): submit + move on to next minute.
            write_kwargs = dict(
                root=root, group=group.name, version=group.version, source="stream", day=target_day,
                frame=out, mode=mode, shard=shard, minute=latest,
            )
            if state.writer is not None:
                state.writer.submit(**write_kwargs)
            else:
                store.write_group(**write_kwargs)
    state.last_write_ms = (time.perf_counter() - write_start) * 1000.0  # measured apart from compute
    metrics.record_group_timings(state.group_timings)  # -> Prometheus histogram, graphed per-group in Grafana
    state.minutes += 1


async def capture(
    url: str,
    symbols: list[str],
    root: str,
    mode: str,
    window: int = DEFAULT_BUFFER_MINUTES,
    day: str | None = None,
    snapshots: dict[str, pl.DataFrame] | None = None,
) -> int:
    """Websocket adapter (mock feed) → the shared core. Runs until the stream closes."""
    state = CaptureState()
    async with websockets.connect(url) as websocket:
        await websocket.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        async for raw in websocket:
            bars = [b for b in json.loads(raw) if b.get("T") == "b"]
            if bars:
                process_bars(state, bars, root, mode, day, window, snapshots)
    return state.minutes


def main() -> None:
    if len(sys.argv) < 5:
        raise SystemExit("usage: python -m quantlib.features.capture <url> <sym,sym> <root> <mock|real> [day]")
    url, symbols, root, mode = sys.argv[1], sys.argv[2].split(","), sys.argv[3], sys.argv[4]
    day = sys.argv[5] if len(sys.argv) > 5 else None
    count = asyncio.run(capture(url, symbols, root, mode, day=day))
    print(f"captured {count} minutes -> {root} (mode={mode})")


if __name__ == "__main__":
    main()
