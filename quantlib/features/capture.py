"""Live capture client — buffers per-minute bars from a websocket stream, computes the bar features
over a trailing window, and writes ``source=stream`` to the store.

SAME code for mock and real: the URL selects the feed (a local mock vs the real data websocket), and
the store root's mode marker ('mock'|'real') guarantees simulated data never lands in the real
store. This is the FP2 live path and the Monday capture service.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime

import polars as pl
import websockets

from quantlib.features import metrics, store
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch
from quantlib.features.incremental import IncrementalEngine

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
# window features diverge live-vs-backfill — a parity break.
#
# Two depth regimes coexist among the groups:
#   - WINDOWED groups (the vast majority) look back a fixed N minutes — the deepest is price_levels'
#     240m, so any buffer >~270m gives them full backfill-equivalent context. momentum_run needs only
#     60m, etc. For these the buffer depth above their window is pure recompute tax, not correctness.
#   - SESSION-CUMULATIVE groups reset at the UTC day boundary and fold the WHOLE session — swing's
#     ``n_pivots_today``/``minutes_since_pivot`` (rust kernel resets at ``minute // 86400``). Backfill
#     folds the entire collected day, so to keep live==backfill the live buffer must reach the first
#     collected bar of the UTC day. Collection starts at premarket open ~08:00 UTC (4:00 ET); at the
#     16:00 ET close (20:00 UTC) that is ~720 minutes back. A 300m buffer truncated swing late-session
#     (n_pivots_today decreased intraday, minutes_since_pivot pinned at 299) — the CRITICAL-2 non-restart
#     facet (docs/DATA_QUALITY_LEDGER.md). 410m (the originally-floated bump) is INSUFFICIENT — measured.
#
# So the buffer is sized to the cumulative groups' need: full premarket-inclusive UTC session (~720m)
# + 30m slack. Cost (measured `profile 1250 720 --latest`): per-group-summed live recompute ~15.6s at
# 1250 tickers/shard (deployed BATCHED path is lower — reduction groups share one marshal), vs the 60s
# minute budget — comfortable. The reduce (universe-wide) ring is capped INDEPENDENTLY by
# ``sharded_capture.reduce_buffer_minutes`` (cross_sectional_rank's declared window + slack), so this
# bump only deepens the per-shard MAP ring where swing lives; the reduce path stays small.
#
# FOLLOW-UP (queued, backlog P1.0 CRITICAL-2): this global depth taxes WINDOWED groups (esp. momentum_run,
# the dominant term, which needs 60m) with recompute they don't use. Eliminate via per-group buffer-depth
# slicing before compute_latest, or a stateful swing accumulator (its kernel is already an O(1)/bar state
# machine — retain the fold across minutes, parity-gated like WindowedSumState). Neither blocks this fix.
DEFAULT_BUFFER_MINUTES = 750

# Sacred parity tolerance (CLAUDE.md ~1e-6 rel). The self-check measures live incremental-vs-batch
# divergence as a MULTIPLE of this tolerance and records a BREACH when it exceeds benign float drift.
# Well-conditioned features track the batch to ~1-2x tolerance; the OLS r2/corr family near a PERFECT fit
# is numerically sensitive — sum-based r2 is a difference of large near-equal sums, and the incremental
# running add/subtract rounds differently from the batch's fresh window sums, so r2≈1 can diverge far
# beyond tolerance. That conditioning sensitivity is SPECIFIC to the incremental path (batch and backfill
# both use fresh window sums and agree), which is exactly what this self-check exists to surface BEFORE
# the fast path is ever trusted as the source. See docs/AUTONOMOUS_BACKLOG.md (P1 #1).
_PARITY_ATOL = 1e-6
_PARITY_RTOL = 1e-6
_PARITY_BREACH_RATIO = 10.0  # divergence beyond 10x tolerance = beyond benign drift -> record a breach


def _incremental_config() -> tuple[bool, bool, bool]:
    """Read the incremental fast-path env switches each minute (cheap; lets ops flip them without a code
    change). Returns (enabled, parity_check, slice_derive). ALL DEFAULT OFF — with nothing set the path is
    byte-identical to the batch (no deploy risk):
    - ``FP_INCREMENTAL=1``    — assemble the batched reduction groups from the incremental running sums.
    - ``FP_INCREMENTAL_PARITY=1`` — compute BOTH batch (the written truth) and incremental each minute and
      record the divergence to Prometheus; the batch output is still what gets written. This is the live
      evidence gate before the fast path is ever trusted as the source.
    - ``FP_INCREMENTAL_SLICE=1`` — use the fast slice derive (per-symbol last-``max_lag+1``-rows tail). This is
      now PARITY-SAFE for sparse symbols (the tail is positionally exact — it reaches each symbol's actual prior
      bars even across minute gaps), gated cell-for-cell vs the whole-buffer derive by tests/test_fp_incremental
      _features.py. DEFAULT OFF pending live A/B under ``FP_INCREMENTAL_PARITY``; the whole-buffer derive (also
      parity-true) is used until then."""
    return (
        os.environ.get("FP_INCREMENTAL") == "1",
        os.environ.get("FP_INCREMENTAL_PARITY") == "1",
        os.environ.get("FP_INCREMENTAL_SLICE") == "1",
    )


def _engine_for(state: "CaptureState", reduce_input: str, batch_groups: list) -> IncrementalEngine:
    """The per-bucket incremental engine, created on first use and held on the state (it seeds lazily from
    the buffer on its first ``step`` and re-seeds itself when a genuinely-new ticker appears)."""
    engine = state.engines.get(reduce_input)
    if engine is None:
        engine = IncrementalEngine(batch_groups)
        state.engines[reduce_input] = engine
    return engine


def _incremental_parity(batch: dict, incremental: dict) -> float:
    """Worst divergence between the batch (truth) and incremental feature frames for one minute, expressed
    as a MULTIPLE of the parity tolerance ``atol + rtol*|a|`` over the symbols and numeric columns present
    in BOTH (<=1 means within tolerance; the absolute floor keeps near-zero values robust). A null in one
    path that is non-null in the other returns ``inf`` — a hard divergence, not a magnitude one."""
    worst = 0.0
    for name, batch_frame in batch.items():
        inc_frame = incremental.get(name)
        if inc_frame is None:
            return float("inf")
        cols = [c for c in batch_frame.columns if c not in ("symbol", "minute") and c in inc_frame.columns]
        joined = batch_frame.select(["symbol", *cols]).join(
            inc_frame.select(["symbol", *cols]), on="symbol", how="inner", suffix="__inc"
        )
        if joined.is_empty():
            continue
        for col in cols:
            a, b = pl.col(col), pl.col(f"{col}__inc")
            if joined.filter(a.is_null() != b.is_null()).height:
                return float("inf")
            ratio = joined.select(
                ((a - b).abs() / (_PARITY_ATOL + _PARITY_RTOL * a.abs())).fill_null(0.0).max()
            ).item()
            if ratio is not None:
                worst = max(worst, float(ratio))
    return worst


class MinuteRing:
    """Append-only ring of per-minute frames, keyed by minute, capped at ``maxlen`` minutes.

    Replaces the per-minute ``concat(whole buffer) -> unique -> sort(minutes) -> filter(recent)``
    rescan (O(full buffer) every minute to append ~one minute's rows) with an O(new-minute) push:
    one slot per distinct minute, oldest evicted past ``maxlen``, a RE-DELIVERED minute OVERWRITES its
    slot (== ``unique(subset=[symbol,minute], keep="last")`` when each batch is one minute, which the
    reader, the mock feed, and stream_sim all guarantee). ``materialize()`` concats the live slots —
    the SAME (symbol, minute) row set the old path produced, so every consumer (which all
    ``sort([symbol, minute])`` internally) is byte-identical. ``last_n(n)`` / ``last_minutes(...)`` concat
    only the requested tail slots — O(new data), not O(90k-row scan)."""

    def __init__(self, maxlen: int, columns: tuple[str, ...] | None = None) -> None:
        self.maxlen = maxlen
        self.columns = columns  # project each minute to this subset (reduce path: close+volume only)
        self._slots: OrderedDict[datetime, pl.DataFrame] = OrderedDict()

    def push(self, new_frame: pl.DataFrame) -> None:
        """Add a minute's rows (one or more minutes). Each distinct minute OVERWRITES its slot (keep-last
        re-delivery), then the oldest minutes beyond ``maxlen`` are evicted."""
        if self.columns is not None:
            new_frame = new_frame.select(self.columns)
        for minute, group in new_frame.group_by("minute", maintain_order=True):
            key = minute[0] if isinstance(minute, tuple) else minute
            self._slots[key] = group  # last delivery of this minute wins, mirroring unique(keep="last")
            self._slots.move_to_end(key)
        while len(self._slots) > self.maxlen:
            self._slots.popitem(last=False)  # evict the oldest minute slot

    def materialize(self) -> pl.DataFrame:
        """The trailing-window frame: concat the live per-minute slots in minute order."""
        return pl.concat(list(self._slots.values()))

    def last_minutes(self, n: int) -> pl.DataFrame:
        """Concat only the last ``n`` minute slots (the short trailing slice consumers need)."""
        slots = list(self._slots.values())
        return pl.concat(slots[-n:])


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
        self.ring: MinuteRing | None = None  # trailing-window bars as a ring of per-minute frames (O(new) append)
        self.accumulated: dict[str, pl.DataFrame] = {}  # one DEDUPED frame per group
        self.minutes = 0
        self.group_timings: dict[str, float] = {}  # last per-group compute ms (first-class live timing)
        self.last_write_ms = 0.0  # time spent WRITING last minute — excluded from the bet-relevant compute latency
        self.writer: StoreWriter | None = None  # set by a live worker -> writes go async, off the compute path
        self.engines: dict[str, IncrementalEngine] = {}  # one incremental engine per reduce_input bucket (FP_INCREMENTAL)

    @property
    def buffer(self) -> pl.DataFrame | None:
        """The materialized trailing-window frame (the ring's per-minute slots concatenated), or None
        before the first minute."""
        return None if self.ring is None else self.ring.materialize()


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
    project_columns: tuple[str, ...] | None = None,
    buffer_minutes: int | None = None,
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
    OFF in production because the durable record is the per-minute store files, not RAM.

    ``project_columns`` keeps the ring projected to a SUBSET of the bar columns (the reader's reduce
    path needs only symbol/minute/close/volume, never the full 7-column frame). ``buffer_minutes`` caps
    the ring depth below ``window`` (the reduce groups' longest window + slack, not the full 300m). Both
    are PARITY-NEUTRAL where used: the dropped columns/older minutes are never read by the groups that
    run there. Default (None/None) = the full 7-column ``window``-deep ring (the map path)."""
    # Append THIS minute's rows into the trailing ring (O(new), not an O(full-buffer) rescan). The ring
    # keeps one frame per minute, overwrites a re-delivered minute (keep-last), and evicts past its depth,
    # so the materialized frame is the SAME (symbol, minute) row set the old concat+unique+filter produced.
    new_frame = pl.DataFrame(
        [
            {"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": float(bar["o"]),
             "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"]), "volume": float(bar["v"])}
            for bar in bars
        ],
        schema=BARS_SCHEMA,
    )
    if state.ring is None:
        depth = buffer_minutes if buffer_minutes is not None else window
        state.ring = MinuteRing(maxlen=depth, columns=project_columns)
    state.ring.push(new_frame)
    frame = state.ring.materialize()
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
    incremental, parity_check, slice_derive = _incremental_config()
    for reduce_input, batch_groups in batchable.items():
        batch_start = time.perf_counter()
        if incremental and not parity_check:
            # Fast path is the source of truth: assemble from the per-bucket incremental running sums via
            # ``step`` (the SAME ``assemble_from_long`` the batch uses — so warmup/flag null handling is
            # byte-identical to the batch, store-parity-true). The Rust emit variants are faster but their
            # warmup representation is not yet gated against the batch, so they are NOT used live (CLAUDE.md).
            batched = _engine_for(state, reduce_input, batch_groups).step(frame, slice_derive=slice_derive)
        else:
            # Batch is the source of truth (default, and during the parity self-check).
            batched = compute_reduction_batch(batch_groups, ctx)
            if incremental and parity_check:
                inc_out = _engine_for(state, reduce_input, batch_groups).step(frame, slice_derive=slice_derive)
                tol_ratio = _incremental_parity(batched, inc_out)
                metrics.record_incremental_parity(reduce_input, tol_ratio, tol_ratio > _PARITY_BREACH_RATIO)
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


def warm_start_enabled() -> bool:
    """``FP_WARM_START=1`` — on capture startup, rehydrate the trailing ring from the session's already-
    settled bars BEFORE the first live minute, so a restart/relaunch no longer begins with an empty buffer
    (CRITICAL-2: an empty ring makes every long-window feature collapse/emit NaN for the first ``window``
    minutes of streaming, and re-corrupts the long windows on every deploy restart). DEFAULT OFF: with the
    flag unset the launch path is byte-identical to today's cold start (no deploy risk); flip it on for the
    one clean restart once it has been exercised."""
    return os.environ.get("FP_WARM_START") == "1"


def warm_start_ring(
    state: CaptureState,
    bars: pl.DataFrame,
    depth: int,
    project_columns: tuple[str, ...] | None = None,
) -> int:
    """Rehydrate ``state.ring`` from ALREADY-FETCHED trailing bars so a restart does not start cold
    (CRITICAL-2). ``bars`` is the ring schema (symbol, minute, open, close, high, low, volume) — typically
    the session's bars from ``backfill_bars`` (Alpaca historical **RAW**, i.e. the SAME unadjusted SIP bars
    the live stream delivers), so the seeded buffer is parity-true: the warmed ring holds exactly the rows
    the live path would itself have accumulated over those minutes, and the first live minute computes with
    full lookback identical to backfill. Keeps only the trailing ``depth`` distinct minutes (the ring's own
    eviction). Pushed minute-ascending so a later RE-DELIVERED live minute keep-last-overwrites its slot
    (the live stream's exact semantics). Returns the number of distinct minutes seeded.

    Idempotent-safe: if the ring already holds minutes, ``push`` merges by the same keep-last rule. No-ops on
    an empty ``bars`` (a relaunch before any session bar exists)."""
    if bars.is_empty():
        return 0
    if state.ring is None:
        state.ring = MinuteRing(maxlen=depth, columns=project_columns)
    # Minute-ascending so the trailing ``depth`` slots survive eviction (push evicts the OLDEST past maxlen).
    state.ring.push(bars.sort(["minute", "symbol"]))
    return len(state.ring._slots)


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
