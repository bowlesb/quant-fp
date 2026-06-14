"""The full Monday streaming flow — trades + quotes + bars -> tick aggregation -> the INCREMENTAL fast
path -> all ~519 features, with the minute-mark compute measured (NOT the batch ``process_bars``).

This is the convergence of the two proven halves:

  * ``tick_capture.enrich_bars_with_ticks`` — the parity-true tick consumer. Each minute's buffered
    trades/quotes are aggregated (threaded per-symbol ``TickState``, live == backfill at the tick layer)
    and merged onto the bar rows to build the enriched ``minute_agg`` the ``trade_flow`` / ``quote_spread``
    groups consume — the SAME columns the historical backfill produces.
  * ``incremental.IncrementalEngine`` — the fast path. Seeded ONCE at session start, it folds each new
    enriched minute into per-(window, symbol, col) running sums (the slice-derive + stateful-regressor V2
    kernel) and emits the reduction features numpy-natively. No whole-buffer rescan, no batch recompute.

Each shard worker, per minute, does exactly the work that precedes a bet and measures it decomposed:
  1. tick-agg  : enrich_bars_with_ticks (trades/quotes -> minute_agg tick columns)
  2. fold      : IncrementalEngine.state.update — fold the new minute's value matrix into the running sums
  3. emit      : emit the reduction features from the running sums + compute the non-reduction groups
                 (calendar/sector/market-context/tick-runlength/...) at-T via ``compute_latest``
The parquet WRITE is deferred (after the bet) and reported separately — it is NOT on the critical path.

The reader subscribes to trades+quotes+bars (the real ``StockDataStream`` against the msgpack mock — the
EXACT Monday client), buckets ticks by exchange-timestamp minute (``aggregates.bucket_minute``), and on a
completed bar-minute routes that minute's bars + trades + quotes to the shard workers. So the wiring is:
raw tape -> per-minute buckets -> enrich -> incremental fold/emit -> features, end to end, parity-true.

MEASURED (10k symbols, 5 trades + 5 quotes/symbol/min, flood, 300m buffer, 32-core box, slowest-shard p99):
  * INCREMENTAL FAST PATH (269 reduction features, the full tick flow), ISOLATED via FP_SIM_FAST_PATH_ONLY,
    16 shards (~625/shard): tick-agg p50 16ms + fold p50 10ms + reduction-emit p50 35ms  =>  p99 = 82ms
    (< 100ms ✅). At 8 shards it is 118ms — more, smaller shards win for the light fast path (the opposite
    of the batch path, where fatter shards won). So the incremental fast path itself clears the 100ms bar.
  * FULL flow (519 features) is ~777ms p99 (was ~910ms before technical+candlestick moved off batch): the
    remaining 211 NON-reduction features (13 groups) still run the batch ``compute_latest`` and dominate
    (non-reduction "rest"). technical+candlestick (26 features) fold on the per-symbol StatefulEngine
    (recursive EMA + lag-ring kinds) — a separate ``stateful emit`` line. The CROSS-SECTIONAL market groups
    are now off the batch path too: ``market_beta`` (21 features) decomposes into market-relative windowed
    reductions (the broadcast-regressor OLS: beta=slope, corr, idio_vol=std·sqrt(1-r2)) and rides the
    incremental fast path as a ReductionGroup, while ``market_context`` (36 features) is a per-minute UNIVERSE
    GATHER (index broadcasts + own-return point lags, O(universe) once) timed as its own ``cross-sectional
    gather`` line. That leaves ~154 NON-reduction features (11 groups: liquidity / price_returns /
    price_levels / distribution / efficiency / multi_day / ...) on the batch ``compute_latest`` — now the
    dominant "rest". (Budget is 60000ms/minute, so the full flow is operationally safe for Monday — the 100ms
    bar is the aspirational fast-path target.)

Usage:  python -m quantlib.features.stream_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from quantlib.aggregates import QuoteTick, TickState, TradeTick, bucket_minute
from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.bench_stream import (
    PORT,
    SESSION_DAY,
    _start_mock,
    synth_daily,
    synth_reference,
    synth_symbols,
)
from quantlib.features.capture import BARS_SCHEMA, DEFAULT_BUFFER_MINUTES
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, emit_numpy
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.real_capture import _shard_snapshots, build_stream
from quantlib.features.sharded_capture import INDEX_SYMBOLS, REDUCE_GROUPS, shard_of
from quantlib.features.stateful import StatefulEngine, StatefulGroup
from quantlib.features.tick_capture import enrich_bars_with_ticks

# The non-reduction groups that still consume the raw per-minute trades tape (tick_runlength's Rust kernel).
TRADES_GROUPS: tuple[str, ...] = ("tick_runlength",)
# Per-symbol stateful groups now on the incremental fold path (StatefulEngine) instead of batch compute_latest.
STATEFUL_GROUPS: tuple[str, ...] = ("technical", "candlestick")
# CROSS-SECTIONAL gather groups: a per-minute UNIVERSE GATHER (index broadcasts + own-return point lags),
# O(universe) once per minute — timed as its own phase, not the per-symbol "rest". market_beta is NOT here:
# it decomposes into market-relative windowed reductions (the broadcast-regressor OLS) and rides the
# incremental fast path as a ReductionGroup. cross_sectional_rank stays a separate full-universe reduce
# (REDUCE_GROUPS) run by the reader, excluded here.
GATHER_GROUPS: tuple[str, ...] = ("market_context",)


def _bucket_ticks_by_symbol_minute(
    trades: list[dict], quotes: list[dict], minute_epoch: int
) -> tuple[dict[str, list[TradeTick]], dict[str, list[QuoteTick]]]:
    """Bin THIS minute's raw trade/quote dicts (keyed by their exchange timestamp's minute) into
    per-symbol lists of the parity-true ``TradeTick`` / ``QuoteTick`` the aggregator consumes. Only ticks
    whose exchange-ts floors to ``minute_epoch`` are kept (the class-H binning the backfill agrees with)."""
    trades_by_symbol: dict[str, list[TradeTick]] = defaultdict(list)
    quotes_by_symbol: dict[str, list[QuoteTick]] = defaultdict(list)
    for trade in trades:
        if bucket_minute(trade["ts_epoch"]) == minute_epoch:
            trades_by_symbol[trade["S"]].append(
                TradeTick(ts_epoch=trade["ts_epoch"], price=trade["p"], size=trade["s"])
            )
    for quote in quotes:
        if bucket_minute(quote["ts_epoch"]) == minute_epoch:
            quotes_by_symbol[quote["S"]].append(
                QuoteTick(ts_epoch=quote["ts_epoch"], bid=quote["bp"], ask=quote["ap"],
                          bid_size=quote["bs"], ask_size=quote["as"])
            )
    return trades_by_symbol, quotes_by_symbol


def _trades_frame(trades_by_symbol: dict[str, list[TradeTick]], minute: datetime) -> pl.DataFrame:
    """A ``trades`` frame (symbol, ts, price, size) for the tick_runlength Rust kernel, from this minute's
    bucketed trades. ts is reconstructed as a UTC datetime from the tick's epoch seconds."""
    rows = []
    for symbol, ticks in trades_by_symbol.items():
        for tick in ticks:
            rows.append({"symbol": symbol, "ts": datetime.fromtimestamp(tick.ts_epoch, tz=timezone.utc),
                         "price": tick.price, "size": tick.size})
    schema = {"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).cast(schema)  # type: ignore[arg-type]


class StreamShardState:
    """One shard's live streaming state: the trailing enriched-minute buffer, the per-symbol TickState
    threaded across minutes (live == backfill at the tick layer), and the seeded IncrementalEngine that
    folds + emits the reduction features. Non-reduction groups run at-T off the same buffer."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.buffer: pl.DataFrame | None = None
        self.tick_states: dict[str, TickState] = {}
        self.engine: IncrementalEngine | None = None
        self.stateful_engines: dict[str, StatefulEngine] = {}  # technical/candlestick on the per-symbol fold path
        self.minutes = 0
        # decomposed per-minute timings (ms) of the bet-relevant work
        self.tick_agg_ms = 0.0
        self.fold_ms = 0.0
        self.emit_ms = 0.0  # reduction emit_numpy + non-reduction compute_latest (the full per-minute emit)
        self.reduction_emit_ms = 0.0  # the INCREMENTAL fast path's emit (emit_numpy off the running sums)
        self.stateful_emit_ms = 0.0  # technical/candlestick via StatefulEngine.step (the per-symbol fold path)
        self.gather_emit_ms = 0.0  # the cross-sectional UNIVERSE GATHER (market_context) — O(universe) at-T
        self.other_emit_ms = 0.0  # the remaining non-reduction groups' at-T compute_latest (calendar/sector/...)
        self.write_ms = 0.0


def _enriched_minute_frame(enriched: list[dict]) -> pl.DataFrame:
    """Build the minute's enriched bar+tick rows into a frame matching the bars schema plus tick columns."""
    schema = {
        **BARS_SCHEMA,
        "n_trades": pl.Float64, "signed_volume": pl.Float64, "mean_spread_bps": pl.Float64,
        "quote_imbalance": pl.Float64, "mean_bid_size": pl.Float64, "mean_ask_size": pl.Float64,
    }
    rows = [
        {
            "symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": float(bar["o"]),
            "close": float(bar["c"]), "high": float(bar["h"]), "low": float(bar["l"]), "volume": float(bar["v"]),
            "n_trades": bar["n_trades"], "signed_volume": bar["signed_volume"],
            "mean_spread_bps": bar["mean_spread_bps"], "quote_imbalance": bar["quote_imbalance"],
            "mean_bid_size": bar["mean_bid_size"], "mean_ask_size": bar["mean_ask_size"],
        }
        for bar in enriched
    ]
    return pl.DataFrame(rows, schema=schema)


def process_stream_minute(
    state: StreamShardState, bars: list[dict], trades: list[dict], quotes: list[dict],
    root: str, mode: str, day: str | None, snapshots: dict | None, *,
    shard: int | None = None, write: bool = True,
) -> None:
    """One shard, one minute of the FULL flow. Decomposed and measured:

      tick-agg : bucket this minute's ticks by exchange-ts and enrich the bars -> minute_agg
      fold     : fold the new enriched minute into the IncrementalEngine's running sums
      emit     : emit the reduction features from the running sums + compute the non-reduction groups at-T
      write    : (deferred, after the bet) append each group's minute to the store

    The IncrementalEngine is seeded on the FIRST minute (replays the buffer) and folded thereafter — never
    a whole-buffer rescan. Parity is guaranteed by construction: the enriched minute_agg is built by the
    same tick consumer the backfill uses, and the engine's fold/emit is the parity-gated fast path."""
    minute = bars[0]["t"]
    minute_dt = datetime.fromisoformat(minute)
    minute_epoch = bucket_minute(minute_dt.timestamp())

    # 1) TICK-AGG: bin ticks to this minute, aggregate per symbol (threaded state), merge onto bars.
    tick_start = time.perf_counter()
    trades_by_symbol, quotes_by_symbol = _bucket_ticks_by_symbol_minute(trades, quotes, minute_epoch)
    enriched = enrich_bars_with_ticks(bars, trades_by_symbol, quotes_by_symbol, state.tick_states)
    new_frame = _enriched_minute_frame(enriched)
    frame = new_frame if state.buffer is None else pl.concat([state.buffer, new_frame])
    frame = frame.unique(subset=["symbol", "minute"], keep="last")
    recent = sorted(frame["minute"].unique())[-state.window :]
    frame = frame.filter(pl.col("minute").is_in(recent))
    state.buffer = frame
    state.tick_agg_ms = (time.perf_counter() - tick_start) * 1000.0

    latest = frame["minute"].max()
    target_day = day or str(latest.date())
    frames = {"minute_agg": frame, **(snapshots or {})}
    ctx = BatchContext(frames=frames)
    selected = [g for g in runnable(frames) if g.name not in REDUCE_GROUPS]
    reduction_groups = [g for g in selected if isinstance(g, ReductionGroup)]
    # FP_SIM_FAST_PATH_ONLY isolates the incremental fast path: skip the 250 non-reduction features (which
    # still run the batch rolling compute_latest, NOT the incremental path) so the fast path's intrinsic
    # 10k latency is measured without their cross-shard core contention. The full-flow run (toggle off) is
    # the honest end-to-end number; this run answers "does the incremental fast path alone hit <100ms".
    non_reduction = [g for g in selected if not isinstance(g, ReductionGroup)]
    fast_path_only = bool(os.environ.get("FP_SIM_FAST_PATH_ONLY"))
    stateful_groups = [] if fast_path_only else [g for g in non_reduction if g.name in STATEFUL_GROUPS]
    gather_groups = [] if fast_path_only else [g for g in non_reduction if g.name in GATHER_GROUPS]
    other_groups = (
        []
        if fast_path_only
        else [g for g in non_reduction if g.name not in STATEFUL_GROUPS and g.name not in GATHER_GROUPS]
    )

    # 2) FOLD: seed once, then fold the new minute's value matrix into the running sums (incremental path).
    fold_start = time.perf_counter()
    if state.engine is None:
        state.engine = IncrementalEngine(reduction_groups)
        state.engine.seed(frame)  # replays the buffer -> establishes symbols + running sums + stateful state
    else:
        assert state.engine.state is not None
        state.engine.state.update(int(latest.timestamp()), state.engine._matrix_at(frame, latest, slice_derive=True))
        state.engine.state.trim()
    state.fold_ms = (time.perf_counter() - fold_start) * 1000.0

    # 3) EMIT: assemble the reduction features from the running sums, then the non-reduction groups at-T.
    emit_start = time.perf_counter()
    outputs: list[tuple[str, str, pl.DataFrame]] = []
    engine = state.engine
    assert engine.state is not None
    latest_frame = frame.filter(pl.col("minute") == latest)
    reduction_emit_start = time.perf_counter()
    reduction_out = emit_numpy(
        engine.groups, engine.state.running, engine.symbols or [], engine.windows, engine.col_index,
        latest_frame, latest, engine.plan, engine.reg_plan,
    )
    for group in reduction_groups:
        outputs.append((group.name, group.version, reduction_out[group.name]))
    state.reduction_emit_ms = (time.perf_counter() - reduction_emit_start) * 1000.0
    # The per-symbol STATEFUL groups (technical/candlestick) via the StatefulEngine fold path — seeded once,
    # then one-minute folds + emit (the recursive EMA / lag-ring kinds), instead of the batch compute_latest.
    stateful_emit_start = time.perf_counter()
    for group in stateful_groups:
        assert isinstance(group, StatefulGroup)
        engine_s = state.stateful_engines.get(group.name)
        if engine_s is None:
            engine_s = StatefulEngine(group)
            state.stateful_engines[group.name] = engine_s
        out = engine_s.step(frame, ctx)
        outputs.append((group.name, group.version, out))
    state.stateful_emit_ms = (time.perf_counter() - stateful_emit_start) * 1000.0
    # The CROSS-SECTIONAL gather groups (market_context) — a per-minute universe gather (index broadcasts +
    # own-return point lags), O(universe) once, NOT per-symbol rolling. Timed apart as the cross-sectional phase.
    gather_emit_start = time.perf_counter()
    for group in gather_groups:
        out = group.compute_latest(ctx)
        outputs.append((group.name, group.version, out))
    state.gather_emit_ms = (time.perf_counter() - gather_emit_start) * 1000.0
    # The remaining non-reduction groups (calendar/sector/tick-runlength/...) at-T — NOT the incremental fast
    # path; timed apart so the fast-path cost is visible against the full-flow cost.
    other_emit_start = time.perf_counter()
    trades_frame = _trades_frame(trades_by_symbol, minute_dt)
    for group in other_groups:
        group_frames = dict(frames)
        if group.name in TRADES_GROUPS:
            group_frames = {**frames, "trades": trades_frame}
        out = group.compute_latest(BatchContext(frames=group_frames))
        outputs.append((group.name, group.version, out))
    state.other_emit_ms = (time.perf_counter() - other_emit_start) * 1000.0
    state.emit_ms = (time.perf_counter() - emit_start) * 1000.0

    # WRITE: deferred — happens AFTER the bet, measured apart and excluded from the per-minute compute.
    write_start = time.perf_counter()
    if write:
        for name, version, out in outputs:
            store.write_group(root=root, group=name, version=version, source="stream", day=target_day,
                              frame=out, mode=mode, shard=shard, minute=latest)
    state.write_ms = (time.perf_counter() - write_start) * 1000.0
    state.minutes += 1


def _bench_log_path(root: str, shard_id: int) -> Path | None:
    if not os.environ.get("FP_BENCH_LOG"):
        return None
    path = Path(root) / "_bench" / f"shard-{shard_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def stream_worker_main(  # pragma: no cover (process entry)
    shard_id: int, n_shards: int, queue, root: str, mode: str, window: int,
    day: str | None, snapshots: dict | None,
) -> None:
    """Persistent shard worker for the streaming sim: drain the queue of (bars, trades, quotes) minute
    batches (already routed to this shard) and run the full incremental flow, logging decomposed latency."""
    state = StreamShardState(window)
    bench_log = _bench_log_path(root, shard_id)
    if bench_log is not None:
        print(f"[stream-worker {shard_id}] up", file=sys.stderr, flush=True)
    processed = 0
    while True:
        batch = queue.get()
        if batch is None:
            if bench_log is not None:
                print(f"[stream-worker {shard_id}] exiting after {processed} minutes", file=sys.stderr, flush=True)
            return
        bars, trades, quotes = batch
        processed += 1
        compute_start = time.perf_counter()
        process_stream_minute(state, bars, trades, quotes, root, mode, day, snapshots, shard=shard_id)
        compute_ms = (time.perf_counter() - compute_start) * 1000.0 - state.write_ms
        if bench_log is not None:
            record = {
                "shard": shard_id, "minute": bars[0]["t"], "ms": compute_ms, "write_ms": state.write_ms,
                "tick_agg_ms": state.tick_agg_ms, "fold_ms": state.fold_ms, "emit_ms": state.emit_ms,
                "reduction_emit_ms": state.reduction_emit_ms, "stateful_emit_ms": state.stateful_emit_ms,
                "gather_emit_ms": state.gather_emit_ms, "other_emit_ms": state.other_emit_ms,
                "fast_path_ms": state.tick_agg_ms + state.fold_ms + state.reduction_emit_ms,
            }
            with bench_log.open("a") as handle:
                handle.write(json.dumps(record) + "\n")


def route_stream_minute(
    bars: list[dict], trades: list[dict], quotes: list[dict], n_shards: int
) -> list[tuple[list[dict], list[dict], list[dict]]]:
    """Partition a minute's bars + trades + quotes by ``hash(symbol) % n_shards``, replicating the index
    ETFs' bars into every shard (the market-context groups need them locally)."""
    index_bars = [bar for bar in bars if bar["S"] in INDEX_SYMBOLS]
    routed: list[tuple[list[dict], list[dict], list[dict]]] = [
        (list(index_bars), [], []) for _ in range(n_shards)
    ]
    for bar in bars:
        if bar["S"] in INDEX_SYMBOLS:
            continue
        routed[shard_of(bar["S"], n_shards)][0].append(bar)
    for trade in trades:
        routed[shard_of(trade["S"], n_shards)][1].append(trade)
    for quote in quotes:
        routed[shard_of(quote["S"], n_shards)][2].append(quote)
    return routed


def run_streaming_sim(  # pragma: no cover (live multiprocess loop)
    symbols: list[str], root: str, mode: str, n_shards: int, window: int, day: str,
    max_minutes: int, snapshots: dict,
) -> None:
    """Reader: own the websocket (real StockDataStream -> mock), subscribe to trades+quotes+bars, buffer
    ticks per minute, and on each completed bar-minute route bars+ticks to the shard workers. The workers
    run the incremental fast path on the enriched flow. (The cross-sectional reduce is excluded here — it
    is the universe-wide gather phase, benchmarked separately in bench_stream.)"""
    threads_per_worker = max(1, (os.cpu_count() or n_shards) // n_shards)
    os.environ["POLARS_MAX_THREADS"] = str(threads_per_worker)

    ctx = mp.get_context("spawn")
    queues = [ctx.Queue() for _ in range(n_shards)]
    workers = [
        ctx.Process(
            target=stream_worker_main,
            args=(i, n_shards, queues[i], root, mode, window, day,
                  _shard_snapshots(snapshots, symbols, i, n_shards)),
            daemon=True,
        )
        for i in range(n_shards)
    ]
    for worker in workers:
        worker.start()

    stream = build_stream()
    pending: dict = {"minute": None, "bars": [], "trades": [], "quotes": [], "done": 0}

    def dispatch() -> None:
        for shard_id, shard_batch in enumerate(
            route_stream_minute(pending["bars"], pending["trades"], pending["quotes"], n_shards)
        ):
            if shard_batch[0]:
                queues[shard_id].put(shard_batch)

    async def on_bar(bar) -> None:  # type: ignore[no-untyped-def]
        minute = bar.timestamp.replace(second=0, microsecond=0)
        if pending["minute"] is not None and minute != pending["minute"] and pending["bars"]:
            dispatch()
            pending["bars"], pending["trades"], pending["quotes"] = [], [], []
            pending["done"] += 1
            if pending["done"] >= max_minutes:
                if os.environ.get("FP_BENCH_LOG"):
                    print(f"[reader] {pending['done']} minutes dispatched; stopping", file=sys.stderr, flush=True)
                for queue in queues:
                    queue.put(None)
                await stream.stop_ws()
                return
        pending["minute"] = minute
        pending["bars"].append(
            {"S": bar.symbol, "o": float(bar.open), "c": float(bar.close), "h": float(bar.high),
             "l": float(bar.low), "v": float(bar.volume), "t": bar.timestamp.isoformat()}
        )

    async def on_trade(trade) -> None:  # type: ignore[no-untyped-def]
        pending["trades"].append(
            {"S": trade.symbol, "p": float(trade.price), "s": float(trade.size),
             "ts_epoch": trade.timestamp.timestamp()}
        )

    async def on_quote(quote) -> None:  # type: ignore[no-untyped-def]
        pending["quotes"].append(
            {"S": quote.symbol, "bp": float(quote.bid_price), "ap": float(quote.ask_price),
             "bs": float(quote.bid_size), "as": float(quote.ask_size), "ts_epoch": quote.timestamp.timestamp()}
        )

    stream.subscribe_trades(on_trade, *symbols)
    stream.subscribe_quotes(on_quote, *symbols)
    stream.subscribe_bars(on_bar, *symbols)
    stream.run()
    if os.environ.get("FP_BENCH_LOG"):
        print("[reader] stream.run() returned; joining workers", file=sys.stderr, flush=True)
    for worker in workers:
        worker.join(timeout=300)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[rank]


def _report(root: str, n_symbols: int, n_shards: int, warmup: int) -> None:
    """Per-minute compute = the slowest shard's bet-relevant work that minute (tick-agg + fold + emit),
    reported p50/p99/max over steady-state (post-warmup) minutes, decomposed."""
    bench = Path(root) / "_bench"
    by_minute: dict[str, list[dict]] = defaultdict(list)
    for shard_file in sorted(bench.glob("shard-*.jsonl")):
        for line in shard_file.read_text().splitlines():
            record = json.loads(line)
            by_minute[record["minute"]].append(record)
    minutes_sorted = sorted(by_minute)

    def critical(metric: str) -> list[float]:
        return [max(rec[metric] for rec in by_minute[minute]) for minute in minutes_sorted][warmup:]

    compute = critical("ms") or [0.0]
    tick_agg = critical("tick_agg_ms") or [0.0]
    fold = critical("fold_ms") or [0.0]
    emit = critical("emit_ms") or [0.0]
    reduction_emit = critical("reduction_emit_ms") or [0.0]
    stateful_emit = critical("stateful_emit_ms") or [0.0]
    gather_emit = critical("gather_emit_ms") or [0.0]
    other_emit = critical("other_emit_ms") or [0.0]
    fast_path = critical("fast_path_ms") or [0.0]
    writes = critical("write_ms") or [0.0]

    def line(label: str, values: list[float]) -> str:
        return (f"    {label:<22} p50={statistics.median(values):7.1f}ms  "
                f"p99={_percentile(values, 99):7.1f}ms  max={max(values):7.1f}ms")

    print(f"\n=== STREAMING SIM (full trades+quotes+bars flow, incremental fast path): "
          f"{n_symbols} symbols, {n_shards} shards (~{n_symbols // n_shards}/shard), "
          f"{len(minutes_sorted)} minutes ({len(compute)} measured post-warmup) ===")
    print("per-minute COMPUTE — slowest shard each minute (the bet-relevant latency):")
    print(line("FULL flow (519 feats)", compute))
    print("  decomposition:")
    print(line("tick-agg", tick_agg))
    print(line("fold (incr update)", fold))
    print(line("reduction emit (290)", reduction_emit))
    print(line("stateful emit (tech+candle)", stateful_emit))
    print(line("cross-sectional gather", gather_emit))
    print(line("non-reduction (rest)", other_emit))
    print(line("[full emit]", emit))
    print("INCREMENTAL FAST PATH only (tick-agg + fold + reduction emit) — the 269 reduction features:")
    print(line("fast-path total", fast_path))
    print("write (deferred, AFTER the bet — NOT on the critical path):")
    print(line("write", writes))
    p99_full = _percentile(compute, 99)
    p99_fast = _percentile(fast_path, 99)
    print(f"\n=> FULL-flow p99 per-minute compute  = {p99_full:7.0f}ms  (bar: < 100ms)  "
          f"{'PASS' if p99_full < 100.0 else 'FAIL'}")
    print(f"=> FAST-PATH  p99 per-minute compute  = {p99_fast:7.0f}ms  (bar: < 100ms)  "
          f"{'PASS' if p99_fast < 100.0 else 'FAIL'}")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: python -m quantlib.features.stream_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]")
    n_symbols, n_shards, measure = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    warmup = int(sys.argv[4]) if len(sys.argv) > 4 else 60
    window = int(sys.argv[5]) if len(sys.argv) > 5 else DEFAULT_BUFFER_MINUTES
    total_minutes = warmup + measure

    symbols = synth_symbols(n_symbols)
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}
    root = os.environ.get("BENCH_ROOT", "/tmp/stream_sim_store")

    os.environ["FP_BENCH_LOG"] = "1"
    os.environ["STREAM_URL_OVERRIDE"] = f"ws://127.0.0.1:{PORT}"
    os.environ.setdefault("ALPACA_KEY_ID", "mock")
    os.environ.setdefault("ALPACA_SECRET_KEY", "mock")
    os.environ.setdefault("MOCK_TRADES_PER_MIN", "5")
    os.environ.setdefault("MOCK_QUOTES_PER_MIN", "5")
    os.environ["MOCK_MINUTES"] = str(total_minutes + 2)

    print(f"streaming {n_symbols} symbols x {total_minutes} minutes (full trades+quotes+bars flow) through "
          f"REAL StockDataStream -> mock (warmup {warmup}, window {window}); root={root}", flush=True)
    _start_mock(total_minutes + 2)
    time.sleep(1.5)
    run_streaming_sim(symbols, root, "mock", n_shards=n_shards, window=window, day=SESSION_DAY,
                      max_minutes=total_minutes, snapshots=snapshots)
    _report(root, n_symbols, n_shards, warmup)


if __name__ == "__main__":
    main()
