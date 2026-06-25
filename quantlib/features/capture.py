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
from typing import cast

import polars as pl
import websockets

from quantlib.features import metrics, store
from quantlib.features.base import BatchContext
from quantlib.features.bus_hook import BusHook, bus_publish_enabled
from quantlib.features.clean_capture import emit_to_frames, minute_frame_to_bars
from quantlib.features.clean_engine import CleanEngine
from quantlib.features.clean_registry import ALL_CLEAN_GROUPS, ALL_CLEAN_INPUT_COLS, CLEAN_VERSION_OF
from quantlib.features.clean_session import build_session
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import attach_reduction_anchors
from quantlib.features.tick_capture import TICK_COLUMNS

BARS_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
}

# When the reader has enriched a minute's bars with aggregated tick columns (n_trades, signed_volume,
# mean_spread_bps, ...), those columns must reach the ``minute_agg`` frame so the trade_flow / quote_spread
# / liquidity groups self-select (``runnable``) and serve them live — exactly as the backfill loader's
# ``minute_agg`` carries them (parity). They are Float64 like the loader's columns; symbols with no ticks
# this minute get null (honest "not collected"), not a fabricated zero.
TICK_SCHEMA = {column: pl.Float64 for column in TICK_COLUMNS}

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
# This global depth would tax WINDOWED groups with recompute they don't use, so momentum_run and
# residual_analysis (the dominant windowed terms, max 60m) override ``compute_latest`` to SLICE the buffer to
# their own trailing window before running the SAME ``compute()`` (FeatureGroup.compute_latest_on_window,
# parity-guarded by tests/test_fp_latest.py). Swing still needs the full premarket-inclusive session (its fold
# resets at the day boundary), which is what keeps this MAP ring deep; a stateful swing accumulator (retain the
# O(1)/bar fold across minutes, parity-gated like WindowedSumState) is the remaining way to shrink it.
DEFAULT_BUFFER_MINUTES = 300

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


def _incremental_config() -> tuple[bool, bool]:
    """Read the incremental fast-path env switches each minute (cheap; lets ops flip them without a code
    change). Returns (parity_check, slice_derive).

    The ``incremental_safe`` reduction groups now ALWAYS assemble from the incremental running sums — the
    default path, value-identical to the batch fresh-sum recompute (verified cell-for-cell on the real-tape
    A/B, docs/INCREMENTAL_READINESS.md), so there is no longer an ``FP_INCREMENTAL`` master switch.
    - ``FP_INCREMENTAL_PARITY=1`` — OPTIONAL, default OFF, MONITORING-ONLY: additionally compute the batch
      each minute and record the incremental-vs-batch divergence to Prometheus
      (``feature_incremental_parity_breach_total``). The incremental output is STILL what gets written — a
      live self-check, NOT a gate.
    - ``slice_derive`` — DEFAULT ON (the fast per-symbol last-``max_lag+1``-rows tail). PARITY-SAFE for sparse
      symbols (the tail is positionally exact — it reaches each symbol's actual prior bars across minute gaps)
      AND it is the verified-clean SEED path: the slice-derive one-shot seed equals the minute-by-minute fold
      cell-for-cell (the ``seed(H);fold(m) == seed(H+m)`` invariant), so a warm-start rehydrate-then-fold is
      value-identical to a cold accumulate. ``FP_INCREMENTAL_SLICE=0`` opts OUT to the whole-buffer derive (a
      DEBUG escape hatch — its one-shot SEED has a known cancellation divergence on the moment/run-length
      power sums, docs/INCREMENTAL_READINESS.md, so it is NOT the live default)."""
    return (
        os.environ.get("FP_INCREMENTAL_PARITY") == "1",
        os.environ.get("FP_INCREMENTAL_SLICE") != "0",
    )


def clean_engine_enabled() -> bool:
    """``FP_CLEAN_ENGINE=1`` — route the per-minute compute through the clean ``CleanEngine`` instead of the OLD
    path (``compute_latest`` + the per-bucket ``IncrementalEngine``). DEFAULT OFF: unset → ``process_bars`` is
    byte-identical to today (zero deploy risk). The clean engine is built ONCE per session from the startup
    symbol set (provably fixed within a session — the live stream subscribes to exactly those symbols) and
    rolls the SAME store rows the OLD path writes. Roll back = unset the flag (no data migration)."""
    return os.environ.get("FP_CLEAN_ENGINE") == "1"


def _session_symbols(snapshots: dict[str, pl.DataFrame], frame: pl.DataFrame) -> list[str]:
    """The FIXED session symbol set the clean engine's index is built over — the union of the held snapshots'
    symbols (``daily``/``reference``/``universe`` all carry the whole session universe) plus this minute's
    symbols as a floor (so a first-minute build before any snapshot is non-empty). Sorted for determinism. The
    set is fixed within a session (subscription-bounded), so building once is safe."""
    symbols: set[str] = set(frame["symbol"].to_list())
    for name in ("daily", "reference", "universe"):
        if name in snapshots and "symbol" in snapshots[name].columns:
            symbols.update(snapshots[name]["symbol"].to_list())
    return sorted(symbols)


def _build_clean_engine(state: "CaptureState", frame: pl.DataFrame, window: int) -> CleanEngine:
    """Build (once per session) the clean engine over the fixed session symbol set + populate its session/static
    from the held snapshots (``build_session``). Held on ``state.clean_engine`` for the session."""
    symbols = _session_symbols(state.snapshots, frame)
    # CleanEngine derives its bar-column set from the groups' input_cols internally (== ALL_CLEAN_INPUT_COLS,
    # which the marshal uses to produce the matching minute dict).
    engine = CleanEngine(list(ALL_CLEAN_GROUPS), symbols, window)
    session, static = build_session(state.snapshots, symbols)
    engine.set_session(session)
    engine.static = static
    return engine


def _clean_outputs(
    state: "CaptureState", frame: pl.DataFrame, window: int, minute_epoch: int
) -> list[tuple]:
    """The clean-engine per-minute compute: build/hold the engine, marshal THIS minute's bars into its numpy
    input, ``emit`` (fold + present-filtered read), and reshape to the per-group ``(symbol, minute, *features)``
    frames the write loop consumes. Returns the same ``(group, out, ms)`` tuples the OLD path produces (timing is
    one bulk measurement split across groups — the engine computes all groups in one pass)."""
    if state.clean_engine is None:
        state.clean_engine = _build_clean_engine(state, frame, window)
    engine = state.clean_engine
    compute_start = time.perf_counter()
    minute_bars = minute_frame_to_bars(frame, ALL_CLEAN_INPUT_COLS, minute_epoch)
    present_symbols, features = engine.emit(minute_bars)
    group_frames = emit_to_frames(present_symbols, features, engine.symbols, minute_epoch)
    per_ms = (time.perf_counter() - compute_start) * 1000.0 / max(len(group_frames), 1)
    return [(group, group_frames[group.name], per_ms) for group in ALL_CLEAN_GROUPS]


def _engine_for(state: "CaptureState", reduce_input: str, batch_groups: list) -> IncrementalEngine:
    """The per-bucket incremental engine, created on first use and held on the state (it seeds lazily from
    the buffer on its first ``step`` and re-seeds itself when a genuinely-new ticker appears)."""
    engine = state.engines.get(reduce_input)
    if engine is None:
        engine = IncrementalEngine(batch_groups, assert_ready_on_seed=state.assert_ready_on_seed)
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
        """The trailing-window frame: concat the live per-minute slots in minute order.

        ``how="diagonal"`` null-fills columns absent from a slot, so a 7-col bar-only slot (e.g. a
        warm-start seed minute from settled bars, which carry no tick enrichment) and a 13-col
        tick-enriched live slot concat cleanly: the seed minute gets NULL tick columns — honest "not
        collected" — exactly the null a settled premarket bar carries in the backfill ``minute_agg``, so
        parity holds. For a homogeneous ring (every slot the same schema) this is byte-identical to the
        plain concat. Without it, mixing 7-col and 13-col slots raises a polars ShapeError (the warm-start
        crash that forced FP_WARM_START off)."""
        return pl.concat(list(self._slots.values()), how="diagonal")

    def last_minutes(self, n: int) -> pl.DataFrame:
        """Concat only the last ``n`` minute slots (the short trailing slice consumers need). ``diagonal``
        for the same heterogeneous-schema reason as ``materialize`` (seed vs tick-enriched slots)."""
        slots = list(self._slots.values())
        return pl.concat(slots[-n:], how="diagonal")


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
        self.ring: MinuteRing | None = (
            None  # trailing-window bars as a ring of per-minute frames (O(new) append)
        )
        self.accumulated: dict[str, pl.DataFrame] = {}  # one DEDUPED frame per group
        self.minutes = 0
        self.group_timings: dict[str, float] = {}  # last per-group compute ms (first-class live timing)
        self.last_write_ms = (
            0.0  # time spent WRITING last minute — excluded from the bet-relevant compute latency
        )
        self.writer: StoreWriter | None = (
            None  # set by a live worker -> writes go async, off the compute path
        )
        self.engines: dict[str, IncrementalEngine] = (
            {}
        )  # one incremental engine per reduce_input bucket (safe groups)
        self.bus_hook: BusHook | None = (
            None  # set lazily when FP_BUS=1 (real mode) -> publishes vectors off-path
        )
        # Set by the warm-start path (``warm_start_ring``) so the FIRST incremental engine seed runs the
        # universal ``assert_ready`` (FULL / legit-not-yet-full / FAILED) + internal invariants against the
        # rehydrated buffer — catching a present-but-not-absorbed fill loudly at init. The populated invariant
        # holds at all times regardless; this just picks the warm-start seed as a convenient call site.
        self.assert_ready_on_seed = False
        self.snapshots: dict[str, pl.DataFrame] = {}  # reference frames (daily, …) held for engine reseed
        # The clean engine, built ONCE per session from the startup symbol set (provably fixed within a session —
        # the live stream is subscribed to exactly those symbols) when FP_CLEAN_ENGINE=1; None otherwise. Its
        # session/static are populated from ``snapshots`` via ``clean_session.build_session``. Replaces the OLD
        # compute path (compute_latest + the IncrementalEngine buckets) with one ``emit()`` per minute.
        self.clean_engine: CleanEngine | None = None

    @property
    def buffer(self) -> pl.DataFrame | None:
        """The materialized trailing-window frame (the ring's per-minute slots concatenated), or None
        before the first minute. The centering anchors are attached the SAME way ``process_bars`` attaches
        them before the engine folds (``attach_reduction_anchors`` from the held ``snapshots``), so the
        incremental engine RESEED history matches the per-minute STEP history cell-for-cell — the
        ``seed(H);fold(m) == seed(H+m)`` parity invariant the reduction groups rely on (a reseed off a raw,
        un-anchored buffer would otherwise raise on volume's ``__anchor_volume`` column)."""
        if self.ring is None:
            return None
        frames = attach_reduction_anchors({"minute_agg": self.ring.materialize(), **self.snapshots})
        return frames["minute_agg"]


def _bars_to_frame(bars: list[dict]) -> pl.DataFrame:
    """Build the ``minute_agg`` frame for a minute's bars (the OHLCV schema, plus the aggregated tick
    columns when the reader enriched them). When ANY bar carries the tick columns, every row gets them
    (null where a symbol had no ticks this minute) so the schema is stable across minutes (the ring
    concats slots) and the trade_flow / quote_spread / liquidity groups self-select. A pure-bars minute
    (no tick subscription) keeps the 7-column OHLCV schema, unchanged."""
    has_ticks = any(column in bar for bar in bars for column in TICK_COLUMNS)
    if has_ticks:
        rows = [
            {
                "symbol": bar["S"],
                "minute": datetime.fromisoformat(bar["t"]),
                "open": float(bar["o"]),
                "close": float(bar["c"]),
                "high": float(bar["h"]),
                "low": float(bar["l"]),
                "volume": float(bar["v"]),
                **{column: bar[column] if column in bar else None for column in TICK_COLUMNS},
            }
            for bar in bars
        ]
        return pl.DataFrame(rows, schema={**BARS_SCHEMA, **TICK_SCHEMA})
    rows = [
        {
            "symbol": bar["S"],
            "minute": datetime.fromisoformat(bar["t"]),
            "open": float(bar["o"]),
            "close": float(bar["c"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "volume": float(bar["v"]),
        }
        for bar in bars
    ]
    return pl.DataFrame(rows, schema=BARS_SCHEMA)


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
    extra_frames: dict[str, pl.DataFrame] | None = None,
    drop_output_symbols: frozenset[str] = frozenset(),
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
    run there. Default (None/None) = the full 7-column ``window``-deep ring (the map path).

    ``extra_frames`` are SINGLE-MINUTE input frames (not the trailing-ring bars) merged into the compute
    context so input-specific groups self-select — the worker passes THIS minute's raw ``trades`` frame
    here so ``tick_runlength`` / ``microstructure_burst`` (InputSpec name="trades") become ``runnable``.
    These groups truncate to the minute internally and emit one (symbol, minute) row, so the per-minute
    frame is exactly their input (no trailing buffer); an absent/empty ``trades`` frame -> they emit no
    rows for the minute (honest 'no trades'), they don't fabricate zeros.

    ``drop_output_symbols`` are symbols this caller COMPUTES but must NOT PERSIST — the sharded executor
    replicates the index ETFs (SPY/QQQ/IWM) into EVERY shard as market-context compute inputs, but only the
    shard that OWNS each index symbol should write it; the others drop it from their output so the store
    holds ONE (symbol, minute) row per broadcast symbol instead of N byte-identical shard copies. The symbols
    stay in the trailing ring and compute context (they are required inputs there) — only the persisted rows
    are filtered, so feature VALUES are unchanged (parity-neutral)."""
    # Append THIS minute's rows into the trailing ring (O(new), not an O(full-buffer) rescan). The ring
    # keeps one frame per minute, overwrites a re-delivered minute (keep-last), and evicts past its depth,
    # so the materialized frame is the SAME (symbol, minute) row set the old concat+unique+filter produced.
    new_frame = _bars_to_frame(bars)
    if state.ring is None:
        depth = buffer_minutes if buffer_minutes is not None else window
        state.ring = MinuteRing(maxlen=depth, columns=project_columns)
    state.ring.push(new_frame)
    if snapshots:
        # Hold the slowly-changing reference frames (notably ``daily``, the volume-anchor source) so the
        # ``buffer`` property re-attaches the SAME anchors on a reseed — keeping the engine seed history
        # identical to the folded step history (parity-critical for a hot-swap reseed of the volume engine).
        state.snapshots = snapshots
    frame = state.ring.materialize()
    latest = cast(datetime, frame["minute"].max())
    target_day = day or str(latest.date())
    frames = {"minute_agg": frame, **(snapshots or {}), **(extra_frames or {})}
    # Attach the per-symbol reduction anchors (volume centering) onto minute_agg from the held ``daily``
    # snapshot BEFORE the engine seeds/folds it and before ``runnable`` is evaluated — the SAME wiring point
    # backfill (materialize._write_all) applies, so the centered-std column is identical in both paths. The
    # batch path centers anyway (shift-invariant, value-additive); the incremental fold reads ``frame``
    # directly (step) so the anchor must live on this exact object. No-op when ``daily`` is absent.
    frames = attach_reduction_anchors(frames)
    frame = frames["minute_agg"]
    ctx = BatchContext(frames=frames)
    outputs: list[tuple] = []
    if clean_engine_enabled():
        # CLEAN-ENGINE PATH (FP_CLEAN_ENGINE=1): one ``emit()`` over the held per-session engine replaces the OLD
        # ``compute_latest`` + ``IncrementalEngine`` buckets below. Same ``(group, out, ms)`` tuples → the write
        # loop is unchanged. ``exclude_groups``/``only_groups`` still apply (the gather phase excludes the xsec
        # groups per shard); the clean engine computes all groups, so filter its outputs here to match.
        minute_epoch = int(latest.timestamp())
        outputs = [
            (group, out, ms)
            for group, out, ms in _clean_outputs(state, frame, window, minute_epoch)
            if group.name not in exclude_groups and (only_groups is None or group.name in only_groups)
        ]
        return _write_outputs(
            state, outputs, root, target_day, mode, shard, latest, accumulate, write, drop_output_symbols
        )
    selected = [
        group
        for group in runnable(frames)
        if group.name not in exclude_groups and (only_groups is None or group.name in only_groups)
    ]
    # Declarative reduction groups sharing an input run in ONE batched marshal+kernel pass (one symbol-code
    # + numpy copy for all of them); every other group runs individually. Both yield (group, out, ms).
    batchable: dict[str, list] = defaultdict(list)
    for group in selected:
        if isinstance(group, ReductionGroup):
            batchable[group.reduce_input].append(group)
        else:
            group_start = time.perf_counter()
            out = group.compute_latest(ctx)  # live = aggregate-at-T (fast where overridden; parity-guarded)
            outputs.append((group, out, (time.perf_counter() - group_start) * 1000.0))
    parity_check, slice_derive = _incremental_config()
    for reduce_input, batch_groups in batchable.items():
        # Split the bucket: ``incremental_safe`` groups ALWAYS ride the running sums (the default,
        # value-identical fast path — verified clean cell-for-cell on the real-tape A/B,
        # docs/INCREMENTAL_READINESS.md); the conditioning-sensitive ``incremental_safe=False`` groups stay on
        # the batch fresh-sum recompute (their incremental-vs-batch corner divergence breaches the parity
        # self-check — see ReductionGroup.incremental_safe). The engine is seeded over the SAFE groups ONLY, so
        # its running sums match exactly what ``step`` assembles. The Rust emit variants are faster but their
        # warmup representation is not yet gated against the batch, so they are NOT used live (CLAUDE.md).
        safe_groups = [group for group in batch_groups if group.incremental_safe]
        unsafe_groups = [group for group in batch_groups if not group.incremental_safe]
        batch_start = time.perf_counter()
        batched: dict[str, pl.DataFrame] = {}
        if safe_groups:
            # Incremental running sums are the SOURCE OF TRUTH for the safe groups (assembled via ``step`` —
            # the SAME ``assemble_from_long`` the batch uses, so warmup/flag null handling is byte-identical
            # and store-parity-true).
            inc_out = _engine_for(state, reduce_input, safe_groups).step(frame, slice_derive=slice_derive)
            batched.update(inc_out)
            if parity_check:
                # FP_INCREMENTAL_PARITY=1 — MONITORING-ONLY self-check (NOT a gate): also run the batch
                # fresh-sum recompute and record the incremental-vs-batch divergence to Prometheus. The output
                # ABOVE is still what is written; this just surfaces any breach
                # (``feature_incremental_parity_breach_total``) without altering the served values.
                batch_truth = compute_reduction_batch(safe_groups, ctx)
                tol_ratio = _incremental_parity(batch_truth, inc_out)
                metrics.record_incremental_parity(reduce_input, tol_ratio, tol_ratio > _PARITY_BREACH_RATIO)
        if unsafe_groups:
            batched.update(compute_reduction_batch(unsafe_groups, ctx))
        per_ms = (time.perf_counter() - batch_start) * 1000.0 / len(batch_groups)
        for group in batch_groups:
            outputs.append((group, batched[group.name], per_ms))

    return _write_outputs(
        state, outputs, root, target_day, mode, shard, latest, accumulate, write, drop_output_symbols
    )


def _group_version(group: object, group_name: str) -> str:
    """The store ``version`` for a group's rows: a legacy group carries its own ``version``; a clean
    ``EngineGroup`` (no version attribute) resolves to its legacy parent's version via ``CLEAN_VERSION_OF`` so
    both engines write the SAME store path."""
    return str(getattr(group, "version", None) or CLEAN_VERSION_OF[group_name])


def _write_outputs(
    state: "CaptureState",
    outputs: list[tuple],
    root: str,
    target_day: str,
    mode: str,
    shard: int | None,
    latest: object,
    accumulate: bool,
    write: bool,
    drop_output_symbols: frozenset[str],
) -> None:
    """The shared write/bus/timing tail — UNCHANGED across the OLD and clean compute paths (both produce the same
    ``(group, out, ms)`` tuples). Persists one ``(symbol, minute)`` row per present symbol per group, records the
    per-group timing, optionally accumulates / publishes to the bus."""
    write_start = time.perf_counter()
    publish_outputs: list[tuple[str, pl.DataFrame]] = []
    bus_on = mode == "real" and bus_publish_enabled()
    for group, out, compute_ms in outputs:
        state.group_timings[group.name] = compute_ms
        if drop_output_symbols and "symbol" in out.columns:
            # Persist-only filter: drop the replicated index symbols this shard does not OWN so the store
            # holds one row per (symbol, minute), not N shard copies. Computed values above are untouched.
            out = out.filter(~pl.col("symbol").is_in(list(drop_output_symbols)))
        if bus_on:
            publish_outputs.append((group.name, out))
        if accumulate:
            prior = state.accumulated.get(group.name)
            state.accumulated[group.name] = (
                out
                if prior is None
                else pl.concat([prior, out]).unique(subset=["symbol", "minute"], keep="last")
            )
        if write:
            # Append THIS minute only (minute-keyed file) — O(1) per tick, idempotent on re-delivery.
            # Off the critical path when a StoreWriter is set (live workers): submit + move on to next minute.
            write_kwargs = dict(
                root=root,
                group=group.name,
                version=_group_version(group, group.name),
                source=store.source_for_mode(mode),
                day=target_day,
                frame=out,
                mode=mode,
                shard=shard,
                minute=latest,
            )
            if state.writer is not None:
                state.writer.submit(**write_kwargs)
            else:
                store.write_group(**write_kwargs)
    state.last_write_ms = (time.perf_counter() - write_start) * 1000.0  # measured apart from compute
    if bus_on:
        # Off the critical path: hand THIS minute's per-symbol frames to the bus thread (assemble+XADD).
        if state.bus_hook is None:
            state.bus_hook = BusHook()
        state.bus_hook.submit(latest, publish_outputs)
    metrics.record_group_timings(
        state.group_timings
    )  # -> Prometheus histogram, graphed per-group in Grafana
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
    # Arm the universal readiness assert at the first incremental engine seed (which folds this rehydrated
    # ring): every window must be populated or legitimately not-yet-full GIVEN the seeded history — a
    # present-but-not-absorbed fill (the warm-start ShapeError class) then raises at init instead of silently
    # under-warming live emissions. The populated invariant is maintained continuously regardless; this is one
    # convenient call site of it.
    state.assert_ready_on_seed = True
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
        raise SystemExit(
            "usage: python -m quantlib.features.capture <url> <sym,sym> <root> <mock|real> [day]"
        )
    url, symbols, root, mode = sys.argv[1], sys.argv[2].split(","), sys.argv[3], sys.argv[4]
    day = sys.argv[5] if len(sys.argv) > 5 else None
    count = asyncio.run(capture(url, symbols, root, mode, day=day))
    print(f"captured {count} minutes -> {root} (mode={mode})")


if __name__ == "__main__":
    main()
