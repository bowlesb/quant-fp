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
  * FULL flow (519 features) is now ~434–448ms p99 (was ~523ms): three more portable groups have moved off
    the batch ``compute_latest`` onto the fast path. ``liquidity`` (15 features) decomposes into
    ADDITIVE-WINDOW reductions (amihud mean,
    roll's autocovariance from four paired sums) + a windowed OLS (Kyle's lambda) and rides the incremental
    fast path as a ReductionGroup — so the ReductionGroup tier is now 305 features. ``price_returns`` (40
    features, the LAG / last-k kind: one LagSpec per window) and ``price_levels`` (21 features, the new
    ROLLING-EXTREMA kind: a per-(symbol, window) monotonic deque for trailing max-high / min-low) fold on the
    per-symbol StatefulEngine, joining technical+candlestick — so the stateful tier is now 87 features. That
    leaves 82 NON-reduction features in 9 groups (asset_flags / calendar / calendar_events /
    microstructure_burst / multi_day_returns / multi_day_vwap / prior_day / round_levels / sector) on the
    batch ``compute_latest`` — the residual "rest" (down from ~154 in 11 groups). market_beta rides the
    incremental fast path as a ReductionGroup; market_context is the per-minute UNIVERSE GATHER (its own
    ``cross-sectional gather`` line). (Budget is 60000ms/minute, so the full flow is operationally safe for
    Monday — the 100ms bar is the aspirational fast-path target.)
  * STATEFUL EMIT moved to Rust + a SHARED coded buffer: ~250ms p99 -> ~125ms p99 (p50 ~150ms -> ~93ms), so
    the FULL flow is now ~305-334ms p99 (was ~440ms). The Rust kernels (quant_tick.rolling_extrema /
    time_lag_gather) take the price_levels extrema and price_returns/candlestick lags off the per-symbol
    Python deque/ring loop. CRUCIAL FINDING: the Python fold was NEVER the real cost (the kernel itself is
    ~1-3ms); the cost is the per-minute WHOLE-BUFFER polars sort + frame assembly each stateful group did. The
    big lever was building the symbol-coded, (symbol, minute)-sorted buffer ONCE per minute and sharing it
    across all four stateful groups (one sort, not four) — ``stateful.coded_buffer`` passed to each
    ``StatefulEngine.step``. The Rust gather then reads numpy off that shared buffer.
  * RUST ASSEMBLE (FP_RUST_ASSEMBLE) took the reduction-emit's CANONICAL column algebra into the
    ``quant_tick.assemble_canonical`` kernel (one pass over the whole running-sum array -> all groups'
    mean/std/sum + OLS slope/corr/r2/mean_y columns, NaN==null by construction, parity-true cell-for-cell vs
    the numpy/polars emit — tests/test_fp_incremental_emit.py). reduction-emit p50 ~50ms -> ~44ms; the
    FAST-PATH (tick-agg + fold + reduction-emit) now clears the bar: ~110ms -> ~88ms p99 (< 100ms ✅).
    CRUCIAL FINDING (micro-profile, 625-sym shard): the canonical algebra was NEVER the cost — the numpy
    canonical build is ~1.4ms and the Rust kernel ~0.4ms. The ~100ms reduction-emit is the POLARS WIDE-FRAME
    BUILD (~42ms: 330 canonical columns -> 13 per-group DataFrames via ``pl.from_numpy`` of a contiguous block
    + join to the latest row) + the per-group ``assemble()`` EXPRESSION EVALUATION (~40ms). The kernel removed
    the (small) canonical cost AND the block ingest replaced 330 per-column ``pl.Series`` copies (~52ms ->
    ~42ms). The remaining reduction-emit floor is polars' per-group assemble eval + join, NOT arithmetic.
  * Full-flow p99 was ~288ms — dominated by the STATEFUL EMIT and the ~82 NON-REDUCTION "rest" features
    (calendar / sector / multi_day) on the batch ``compute_latest``, NOT the reduction emit.
  * EMIT-CONSOLIDATE (quantlib.features.consolidated) took the "rest" phase off the per-group
    ``compute_latest`` loop: the residual cost was per-group POLARS FRAME-BUILD, not arithmetic — ~8 of those
    groups each filter+select+with_columns their own frame per minute. Two families that share an index are
    now computed in ONE shared pass each: (a) the POINT-IN-TIME groups (calendar / calendar_events / sector /
    asset_flags / round_levels, 30 feats) build the latest minute's (symbol, minute, close) index + ONE
    reference join and run all five groups' expressions in one ``with_columns`` (isolated 625-sym profile:
    35.9ms -> 3.0ms); (b) the DAILY-BROADCAST groups (multi_day_returns / multi_day_vwap / prior_day, 48
    feats) merge their three per-(symbol, date) daily frames ONCE per session (cached on the daily-snapshot
    identity) and do a SINGLE broadcast-join of the latest minute per minute instead of three (14.8ms ->
    10.6ms). SCHEDULING change only — each group exposes its column expressions (``exprs()``) and the
    consolidated emit applies the SAME expressions on the shared frame, so output is byte-identical
    (tests/test_fp_consolidated.py: consolidated == compute_latest == compute().last, cell-for-cell). MEASURED
    (10k, 16 shards, flood, same machine state, back-to-back vs the rust-assemble base): the non-reduction
    "rest (82)" phase 102ms -> 28ms p99, and FULL-flow p99 215ms -> 135ms (the entire ~80ms full-flow drop is
    the rest-phase collapse). The reduction-emit + stateful-emit phases are unchanged. The largest remaining
    full-flow phase is now the REDUCTION EMIT (~60ms p99, the polars per-group assemble eval + wide-frame
    join) ~tied with the STATEFUL EMIT (~43ms p99); the rest phase is no longer a top lever. 519 full-flow is
    NOT yet under 100ms (~135ms p99) — the floor is the two remaining emit tiers, both polars frame/expr
    bound, not arithmetic.
  * STATEFUL-EMIT CONSOLIDATE (quantlib.features.stateful.emit_stateful + technical's coded reduction). The
    four stateful groups built their own per-symbol state frame + assemble per minute; emit_stateful folds
    every engine's state off the ONE shared coded buffer and runs ALL groups' assemble in one pass, sliced per
    group (byte-identical, tests/test_fp_stateful_emit.py). CRUCIAL FINDING (isolated 625-sym micro-profile):
    the consolidated assemble pass was ~0ms — UNLIKE the cheap tier, the stateful-emit cost was NOT the
    per-group assemble. It was technical's per-minute RSI/SMA reduction RECOMPUTE (its own prep sort + a
    lagged() self-join + two kernel calls each re-sorting/re-marshaling the whole buffer, ~68ms) plus a
    redundant whole-buffer sort to read technical's at-T row (~25ms, EMA-only groups skipped the shared coded
    buffer). Both now reuse the ONE coded buffer: technical.reduction_columns_from_coded derives gain/loss from
    the time-based prior close (gappy-safe) + ONE windowed_sums pass (68ms -> 5ms), and _state_row reads the
    at-T row off the already-sorted coded frame for ALL stateful groups (25ms -> ~0ms). Isolated stateful tier
    ~75ms -> ~35ms p50; byte-identical to the certified reduction_columns on dense AND gappy streams.
  * CLEAN UNCONTENDED MEASUREMENT (10k, flood, 5 trades+5 quotes/sym/min, this machine, only-heavy-job,
    median of 3 runs, steady-state post-warmup). At 32 shards (~312/shard — more, smaller shards win, the box
    is 32 cores so this minimizes per-shard work without oversubscribing): FULL-flow p50 ~125ms / p99 ~177ms;
    decomposition tick-agg ~21ms / fold ~12ms / reduction-emit ~43ms / STATEFUL-emit ~44ms / rest ~18ms /
    gather ~9ms (p50). The FAST PATH (tick-agg+fold+reduction-emit, 305 reduction feats) clears the bar:
    ~95-98ms p99 (PASS). At 16 shards (~625/shard) the same flow is ~160ms p50 / ~230ms p99 — fatter shards
    lose for this light-per-symbol flow. VERDICT: the full 519-feature flow does NOT clear <100ms at 10k even
    uncontended — the realistic floor is ~120-125ms p50, set by the SUM of the per-minute phases (they run
    sequentially per shard), with NO single dominant tier anymore: reduction-emit (~43ms) and stateful-emit
    (~44ms) are tied, both POLARS per-group assemble-eval + wide-frame ingest/join bound (the canonical/kernel
    arithmetic is ~1-3ms — proven repeatedly), on top of an irreducible tick-agg (~21ms, the parity-true tick
    aggregation + buffer concat/unique) + fold (~12ms). Getting all 519 under 100ms would require collapsing
    the two emit tiers' per-group polars passes into a single numpy-native wide emit (bypassing the per-group
    assemble() expression eval), or moving technical's reductions fully onto the incremental running-sum tier
    — both larger changes than this scheduling consolidation. The fast-path 305-feature flow IS under 100ms.
  * UNIFIED REDUCTION EMIT (declarative.emit_rust_unified, wired via IncrementalEngine.step_rust_unified). The
    reduction tier's emit_rust ran the ONE assemble_canonical kernel then built a SEPARATE polars frame +
    assemble() pass per reduction group (~13 per-group frame-builds + joins/minute — the reduction-emit floor;
    the canonical algebra is ~1-3ms). emit_rust_unified ingests the kernel's FULL contiguous canonical block in
    ONE pl.from_numpy (every canonical column name is unique across the 13 groups — verified), joins the UNION
    of all groups' __pt_ point columns (deduped by name; colliding point names carry IDENTICAL exprs — verified),
    evaluates ALL groups' assemble() exprs in ONE with_columns, and slices each group's features back out — the
    same scheduling lever the cheap-tier (consolidated.py) and stateful-tier (emit_stateful) already applied.
    BYTE-IDENTICAL (tol 0) to per-group emit_rust / polars step / batch compute_latest for ALL 13 reduction
    groups (tests/test_fp_unified_emit.py). MEASURED (10k, 32 shards, flood, this machine, only-heavy-job, median
    of 3, warmup 120 so the buffer is realistic): reduction-emit p50 ~43ms -> ~30ms (~13ms / ~30% off that
    phase), and FULL-flow p50 ~125ms -> ~116ms (p99 ~177ms -> ~183ms, noise). VERDICT: still NOT <100ms for all
    519 — the STATEFUL EMIT (~47ms p50) is now the single dominant phase. A standalone micro-profile (312 sym,
    deep buffer) decomposes that ~47ms NOT into per-group assemble (already consolidated, ~0ms) but into
    fold_and_state: technical ~33ms (its per-minute RSI/Bollinger/SMA windowed-reduction RECOMPUTE) + price_levels
    ~19ms + candlestick ~15ms + price_returns ~13ms (the Rust extrema/lag gathers + EMA folds) + the shared
    coded-buffer sort ~9ms. So the unified-assemble lever is EXHAUSTED for the stateful tier — its cost is the
    state-frame BUILD, not the assemble eval. The honest realistic floor with this sharded-polars architecture is
    ~tick-agg (~23ms, parity-true tick agg + buffer concat/unique) + fold (~12ms) + reduction-emit (~30ms, now
    near its single-pass minimum) + stateful-emit (~44ms, dominated by technical's reduction recompute) + rest
    (~19ms) + gather (~10ms) ≈ 115ms p50, all phases sequential per shard. Crossing <100ms for ALL 519 needs a
    FUNDAMENTALLY different stateful path — move technical's RSI/Bollinger/SMA reductions onto the incremental
    running-sum tier (so they fold like the reduction groups instead of recomputing each minute) and/or a fully
    numpy/Rust state-frame assembly that bypasses the per-engine polars state-row build — NOT another scheduling
    consolidation. The fast-path 305-feature reduction flow remains under 100ms (~94ms p99).
  * TECHNICAL FOLD (stateful.ReductionSpec / ReductionFoldState + technical.reduction_columns_from_sums). The
    last per-minute RECOMPUTE on the stateful tier — technical re-running its RSI/Bollinger/SMA windowed-
    reduction kernel over the WHOLE buffer every minute (reduction_columns_from_coded) — moves onto the
    incremental running-sum tier: a per-(window, symbol, col) WindowedSumState (the SAME class the 305
    reduction features fold on) that ADDS the new minute and EXPIRES minutes leaving each window, plus a
    per-symbol TIME-based prior close so RSI's gain/loss stays gappy-safe. SMA = windowed mean, Bollinger =
    mean ± k·std, RSI = ratio of 14m avg gain/loss now FOLD (O(symbols × windows × cols)) instead of
    recomputing. MACD stays on the EMAState (genuinely recursive). The folded and certified paths share ONE
    column-algebra (_reduction_columns_from_long), so they are byte-identical by construction: RSI byte-exact
    (sums cancel), SMA/std within the 1e-9 incremental-sum tolerance the reduction tier already uses (the same
    WindowedSumState drift, bounded by per-session re-seed). Gated on dense AND gappy streams
    (tests/test_fp_stateful_emit.py::test_technical_folded_reduction_matches_certified), and emit_stateful ==
    per-group step == backfill still holds. MEASURED — ISOLATED (312 sym/shard, deep buffer, single process):
    technical's fold_and_state ~27ms -> ~6ms p50 (the ~14ms whole-buffer kernel recompute removed + the
    state-row build); emit_stateful over all four engines ~20ms p50. CLEAN 10k (32 shards, flood, 5t+5q/sym/min,
    this machine, median of 5 interleaved A/B runs vs the unified-emit base): stateful-emit p50 ~48ms -> ~45.5ms,
    FULL-flow p50 ~136ms -> ~131ms, p99 noise-dominated (~210-250ms both; the new path's interleaved p99 ran
    slightly LOWER, 200/211 vs 226/212). VERDICT: the architectural recompute->fold win is real and proven in
    isolation (~21ms off technical's per-minute work), but at 32 concurrent shards the full flow is
    MEMORY-BANDWIDTH / contention bound, NOT compute bound — removing technical's ~14ms isolated recompute yields
    only ~3-5ms of wall time, because the stateful-emit cost under 32x concurrency is the per-engine polars
    state-frame BUILD + the cross-process memory pressure, not the arithmetic. So the full 519-feature flow is
    STILL ~131ms p50 / ~210-250ms p99 — NOT under 100ms. The p99 tail is slowest-shard jitter across 32 shards
    (each shard's worst minute lands on a different wall slice) and is an ARCHITECTURAL FLOOR of the sharded-
    polars emit: every per-minute phase is a polars frame-build contending for the same memory bandwidth across
    32 processes, so the tail is set by the unluckiest shard×minute, not by any one phase's median. Cracking
    <100ms for ALL 519 needs a fundamentally different emit — a fully numpy/Rust state-frame assembly that
    bypasses the per-engine polars state-row build entirely (the reduction tier already proved this with the
    assemble_canonical kernel; the stateful tier's state-row build is the remaining polars cost), or fewer/
    fatter shards traded against per-shard compute. (Fast-path 305-feature p50 stays ~75ms; its p99 ~107-114ms
    on this more-contended machine state is unchanged-code machine contention, not a regression from this fold.)

  * IPC BYTES ROUTING (FP_IPC_MSGPACK, the reader->shard transit — NOT feature compute). The single-threaded
    reader routes each completed minute's ~110k bar/trade/quote dicts to the 32 shard queues; OFF, mp.Queue
    PICKLES each shard's dict-list in the reader's feeder threads (the measured ~120ms transit — confirmed
    here at p50 ~133ms / p99 ~162ms, route+put). ON, the reader instead msgpack-PACKS each shard's slice to
    ONE bytes blob (mp.Queue then only memcpy-pickles the bytes — near-free) and each WORKER msgpack-UNPACKS
    its OWN slice in parallel across the 32 cores. PARITY-NEUTRAL: msgpack roundtrips the flat str/int/float
    dicts byte-identically, so process_stream_minute receives the SAME dicts either way — feature values are
    unchanged (tests/test_fp_stream_sim.py::test_msgpack_transport_roundtrips_routed_batches_byte_identically).
    MEASURED (10k, 32 shards, paced mock MOCK_INTERVAL_SEC=1.5, 5t+5q/sym/min, this machine, 20 post-warmup
    minutes, back-to-back A/B): reader route+put p50 132.7ms -> 111.1ms (~22ms reclaimed) / p99 161.8ms ->
    132.7ms (~29ms reclaimed); the worker pays +6.6ms p50 / +8.0ms p99 of decode but IN PARALLEL across the 32
    shards, so the net wall-time off the reader bottleneck is ~22ms p50 / ~29ms p99. The p99 TAIL compressed
    across the whole flow (moving deserialization off the reader's GIL-bound feeder threads removes cross-
    process stalls): FULL-flow p99 219ms -> 162ms, and the FAST PATH (305 reduction feats) p99 110ms -> 91ms —
    now UNDER 100ms (PASS). FULL-flow p50 is ~unchanged (132ms, expected — the transit overlaps the workers'
    per-minute compute under pacing, so it is not on the serial critical path at p50; the win shows in the
    transit cost itself and the contention-driven p99 tail). RESIDUAL reader-side cost: the route_stream_minute
    partition iterates all ~110k dicts single-threaded AND the msgpack pack is still single-threaded in the
    reader (~111ms p50) — packing is faster than pickle but the reader's per-object iteration is the floor.
    Crossing below that needs partitioning WITHOUT a full per-object pass (a columnar/Rust split) or moving the
    pack off the reader thread — a larger change than this transport swap.

Usage:  python -m quantlib.features.stream_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]
        FP_IPC_MSGPACK=1 selects the bytes transport (A/B vs the default pickle path).
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

import msgpack
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
from quantlib.features.capture import BARS_SCHEMA, DEFAULT_BUFFER_MINUTES, MinuteRing
from quantlib.features.compare import runnable
from quantlib.features.consolidated import (
    DAILY_BROADCAST_GROUPS,
    POINT_IN_TIME_GROUPS,
    emit_daily_broadcast,
    emit_point_in_time,
)
from quantlib.features.declarative import (
    _USE_RUST_ASSEMBLE,
    ReductionGroup,
    emit_numpy,
    emit_rust,
    emit_rust_unified,
    resolve_points,
)
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.real_capture import _reader_bench_path, _shard_snapshots, build_stream
from quantlib.features.sharded_capture import INDEX_SYMBOLS, REDUCE_GROUPS, shard_of
from quantlib.features.stateful import (
    StatefulEngine,
    StatefulGroup,
    coded_buffer,
    emit_stateful,
)
from quantlib.features.tick_capture import enrich_bars_with_ticks

# The non-reduction groups that still consume the raw per-minute trades tape (tick_runlength's Rust kernel).
TRADES_GROUPS: tuple[str, ...] = ("tick_runlength",)
# Per-symbol stateful groups now on the incremental fold path (StatefulEngine) instead of batch compute_latest:
# technical/candlestick (recursive EMA + lag-ring kinds), price_returns (lag/last-k kind), price_levels
# (rolling-extrema kind). liquidity is NOT here — it decomposes into additive-window reductions + an OLS
# (Kyle's lambda) and rides the incremental fast path as a ReductionGroup.
STATEFUL_GROUPS: tuple[str, ...] = ("technical", "candlestick", "price_returns", "price_levels")
# CROSS-SECTIONAL gather groups: a per-minute UNIVERSE GATHER (index broadcasts + own-return point lags),
# O(universe) once per minute — timed as its own phase, not the per-symbol "rest". market_beta is NOT here:
# it decomposes into market-relative windowed reductions (the broadcast-regressor OLS) and rides the
# incremental fast path as a ReductionGroup. cross_sectional_rank stays a separate full-universe reduce
# (REDUCE_GROUPS) run by the reader, excluded here.
GATHER_GROUPS: tuple[str, ...] = ("market_context",)

# IPC transport flag (A/B). Default OFF = the original path: the reader hands each shard's
# (bars, trades, quotes) tuple-of-dicts to mp.Queue, which PICKLES ~100k dicts across 32 queues in the
# single reader's feeder threads (the measured ~120ms reader->shard transit). With FP_IPC_MSGPACK=1 the
# reader instead msgpack-PACKS each shard's batch to a single bytes blob (mp.Queue then only memcpy-pickles
# the bytes — near-free) and each worker msgpack-UNPACKS its OWN slice in parallel across the 32 cores.
# PARITY-NEUTRAL: msgpack roundtrips the flat str/int/float dicts byte-identically, so process_stream_minute
# receives the SAME dicts either way — only the transport differs, feature values are unchanged.
_USE_IPC_MSGPACK = bool(os.environ.get("FP_IPC_MSGPACK"))


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
        self.ring: MinuteRing | None = None  # trailing enriched-minute frames as a ring (O(new) append)
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

    @property
    def buffer(self) -> pl.DataFrame | None:
        """The materialized trailing-window frame (the ring's per-minute slots concatenated), or None
        before the first minute — the same frame the old ``state.buffer`` field held."""
        return None if self.ring is None else self.ring.materialize()


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
    # Append this enriched minute into the trailing ring (O(new), not an O(full-buffer) concat+unique+filter
    # rescan). The materialized frame is the SAME (symbol, minute) row set the old path produced.
    if state.ring is None:
        state.ring = MinuteRing(maxlen=state.window)
    state.ring.push(new_frame)
    frame = state.ring.materialize()
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
    # CONSOLIDATED families: the point-in-time groups (one shared minute-frame pass) and the
    # daily-broadcast groups (one broadcast-join), pulled off the per-group compute_latest loop.
    pit_groups = [] if fast_path_only else [g for g in non_reduction if g.name in POINT_IN_TIME_GROUPS]
    daily_groups = [] if fast_path_only else [g for g in non_reduction if g.name in DAILY_BROADCAST_GROUPS]
    consolidated_names = set(POINT_IN_TIME_GROUPS) | set(DAILY_BROADCAST_GROUPS)
    other_groups = (
        []
        if fast_path_only
        else [
            g
            for g in non_reduction
            if g.name not in STATEFUL_GROUPS
            and g.name not in GATHER_GROUPS
            and g.name not in consolidated_names
        ]
    )

    # 2) FOLD: seed once, then fold the new minute's value matrix into the running sums (incremental path).
    fold_start = time.perf_counter()
    if state.engine is None:
        state.engine = IncrementalEngine(reduction_groups)
        state.engine.seed(frame)  # replays the buffer -> establishes symbols + running sums + stateful state
    else:
        assert state.engine.state is not None
        # The fold's slice-derive tails each symbol's last max_lag+1 ROWS. For this sim's DENSE feed (every
        # symbol prints every minute) the last DERIVE_SLICE+1 minute slots contain exactly those rows, so
        # handing just those slots is equivalent to handing the whole buffer — and far cheaper. (The live
        # ``step`` path hands the whole buffer, the parity-safe source for sparse symbols.)
        fold_slice = state.ring.last_minutes(IncrementalEngine.DERIVE_SLICE + 1)
        state.engine.state.update(int(latest.timestamp()), state.engine._matrix_at(fold_slice, latest, slice_derive=True))
        state.engine.state.trim()
    state.fold_ms = (time.perf_counter() - fold_start) * 1000.0

    # 3) EMIT: assemble the reduction features from the running sums, then the non-reduction groups at-T.
    emit_start = time.perf_counter()
    outputs: list[tuple[str, str, pl.DataFrame]] = []
    engine = state.engine
    assert engine.state is not None
    # The reduction emit needs the latest-minute rows carrying the precomputed ``__pt_<name>`` point
    # columns. Resolve them over the WHOLE buffer (gap-safe positive-lag shift, matching backfill) — the
    # SAME thing the deployed ``compute_reduction_batch`` feeds emit (declarative.resolve_points): the
    # ring's newest slot alone has null lag-points (CRITICAL-1), so emit_rust_unified/emit_numpy would
    # fail to find ``__pt_*``. Resolved over ``engine.groups`` so the point-column union matches emit.
    latest_frame = resolve_points(engine.groups, frame, latest)
    reduction_emit_start = time.perf_counter()
    if _USE_RUST_ASSEMBLE:
        # UNIFIED single-pass emit: assemble EVERY reduction group's features in ONE shared wide-frame pass
        # (one kernel + one canonical ingest + one shared point-select + one with_columns), instead of the
        # ~13 per-group polars frame-builds + assemble passes — byte-identical (tests/test_fp_unified_emit.py).
        reduction_out = emit_rust_unified(
            engine.groups, engine.state.running, engine.symbols or [], engine.asm_plan, latest_frame, latest
        )
    else:
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
    # Build the symbol-coded, (symbol, minute)-sorted buffer ONCE and share it across every stateful group's
    # Rust extrema/lag gather — the whole-buffer sort is the stateful-emit's real cost, so it is paid once,
    # not once per group. (Valid because the stateful groups' ``prepare`` is identity over the bar columns.)
    shared_coded = coded_buffer(frame, latest) if stateful_groups else None
    stateful_engines: list[StatefulEngine] = []
    stateful_versions: dict[str, str] = {}
    for group in stateful_groups:
        assert isinstance(group, StatefulGroup)
        engine_s = state.stateful_engines.get(group.name)
        if engine_s is None:
            engine_s = StatefulEngine(group)
            state.stateful_engines[group.name] = engine_s
        stateful_engines.append(engine_s)
        stateful_versions[group.name] = group.version
    if stateful_engines:
        # CONSOLIDATED: every stateful group's state frame folded + built off the ONE shared coded buffer,
        # then ALL groups' assemble exprs evaluated in one shared pass and sliced per group (byte-identical
        # to the per-group step) — the same scheduling lever the cheap-tier consolidation applied.
        for name, out in emit_stateful(stateful_engines, frame, ctx, coded=shared_coded).items():
            outputs.append((name, stateful_versions[name], out))
    state.stateful_emit_ms = (time.perf_counter() - stateful_emit_start) * 1000.0
    # The CROSS-SECTIONAL gather groups (market_context) — a per-minute universe gather (index broadcasts +
    # own-return point lags), O(universe) once, NOT per-symbol rolling. Timed apart as the cross-sectional phase.
    gather_emit_start = time.perf_counter()
    for group in gather_groups:
        out = group.compute_latest(ctx)
        outputs.append((group.name, group.version, out))
    state.gather_emit_ms = (time.perf_counter() - gather_emit_start) * 1000.0
    # The remaining non-reduction groups (tick-runlength/microstructure-burst/...) at-T — NOT the
    # incremental fast path; timed apart so the fast-path cost is visible against the full-flow cost.
    # The point-in-time + daily-broadcast families are consolidated into one shared pass each (instead
    # of one frame-build per group), since their per-group polars frame-build was the residual "rest" cost.
    other_emit_start = time.perf_counter()
    consolidated_versions = {g.name: g.version for g in (*pit_groups, *daily_groups)}
    if pit_groups:
        for name, out in emit_point_in_time(pit_groups, ctx).items():
            outputs.append((name, consolidated_versions[name], out))
    if daily_groups:
        for name, out in emit_daily_broadcast(daily_groups, ctx).items():
            outputs.append((name, consolidated_versions[name], out))
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
        # FP_IPC_MSGPACK: the reader sent this shard's slice as a single msgpack bytes blob; decode it HERE,
        # in parallel across the 32 worker processes, instead of the reader pickling 32 dict-lists serially.
        # Measured apart (ipc_decode_ms) — it is the worker-side half of the transit, and it is NOT
        # feature compute, so it is excluded from the per-minute "ms" the same way the write is.
        ipc_decode_ms = 0.0
        if isinstance(batch, (bytes, bytearray)):
            decode_start = time.perf_counter()
            bars, trades, quotes = msgpack.unpackb(batch)
            ipc_decode_ms = (time.perf_counter() - decode_start) * 1000.0
        else:
            bars, trades, quotes = batch
        processed += 1
        compute_start = time.perf_counter()
        process_stream_minute(state, bars, trades, quotes, root, mode, day, snapshots, shard=shard_id)
        compute_ms = (time.perf_counter() - compute_start) * 1000.0 - state.write_ms
        if bench_log is not None:
            record = {
                "shard": shard_id, "minute": bars[0]["t"], "ms": compute_ms, "write_ms": state.write_ms,
                "ipc_decode_ms": ipc_decode_ms,
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
    reader_bench = _reader_bench_path(root)  # records the reader-side route+IPC-put transit per minute

    def dispatch() -> None:
        # The reader-side half of the reader->shard transit: partition the minute by shard, then hand each
        # shard its slice across mp.Queue. OFF: queue.put pickles the dict-list (serial, in the reader).
        # ON (FP_IPC_MSGPACK): the reader msgpack-PACKS each slice to bytes (faster than pickle, and the
        # subsequent queue.put only memcpy-pickles the bytes), and the WORKER unpacks in parallel.
        route_start = time.perf_counter()
        routed = route_stream_minute(pending["bars"], pending["trades"], pending["quotes"], n_shards)
        for shard_id, shard_batch in enumerate(routed):
            if not shard_batch[0]:
                continue
            if _USE_IPC_MSGPACK:
                queues[shard_id].put(msgpack.packb(shard_batch))
            else:
                queues[shard_id].put(shard_batch)
        if reader_bench is not None:
            transport = "msgpack-bytes" if _USE_IPC_MSGPACK else "pickle-dicts"
            with reader_bench.open("a") as handle:
                handle.write(json.dumps({"minute": pending["minute"].isoformat(), "transport": transport,
                                         "route_put_ms": (time.perf_counter() - route_start) * 1000.0}) + "\n")

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

    def critical_opt(metric: str) -> list[float]:
        return [max(rec.get(metric, 0.0) for rec in by_minute[minute]) for minute in minutes_sorted][warmup:]

    compute = critical("ms") or [0.0]
    ipc_decode = critical_opt("ipc_decode_ms") or [0.0]
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
    print(line("reduction emit (305)", reduction_emit))
    print(line("stateful emit (87 feats)", stateful_emit))
    print(line("cross-sectional gather", gather_emit))
    print(line("non-reduction (rest, 82)", other_emit))
    print(line("[full emit]", emit))
    print("INCREMENTAL FAST PATH only (tick-agg + fold + reduction emit) — the 305 reduction features:")
    print(line("fast-path total", fast_path))
    print("write (deferred, AFTER the bet — NOT on the critical path):")
    print(line("write", writes))
    print("reader->shard TRANSIT (NOT feature compute — the IPC-routing cost this branch targets):")
    reader_file = bench / "reader.jsonl"
    if reader_file.exists():
        reader_records = [json.loads(text) for text in reader_file.read_text().splitlines()]
        route_put = [rec["route_put_ms"] for rec in reader_records][warmup:] or [0.0]
        transport = reader_records[-1]["transport"] if reader_records else "?"
        print(line(f"reader route+put [{transport}]", route_put))
    print(line("worker msgpack-decode", ipc_decode))
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
