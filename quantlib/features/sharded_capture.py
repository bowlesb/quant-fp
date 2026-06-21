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
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import polars as pl

from quantlib.aggregates import QuoteTick, TickState, TradeTick, bucket_minute
from quantlib.features import latency_drilldown, metrics
from quantlib.features.speculative import (
    SpeculativeMinuteState,
    prepass_aggregate,
    speculative_enabled,
    tail_fold_aggregate,
)
from quantlib.features.capture import (
    CaptureState,
    StoreWriter,
    process_bars,
    warm_start_enabled,
    warm_start_ring,
)
from quantlib.features.registry import REGISTRY
from quantlib.features.tick_capture import enrich_bars_with_ticks, trades_frame

# Index ETFs replicated to every shard so market-context/beta compute locally (tiny, ~3 symbols).
INDEX_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
# Each shard worker exposes /metrics here (Prometheus scrapes BASE + shard_id) — keep in sync with the
# feature-capture job in config/prometheus/prometheus.yml.
WORKER_METRICS_BASE_PORT = 9201
# Groups that depend on the WHOLE universe at a minute — run in the gather phase, not per shard. All are
# universe-wide GATHER reduces: cross_sectional_rank percentiles over all symbols; breadth counts the
# up/down fraction of the whole market (+ each sector); market_turbulence means |trailing return| and
# realized vol over the whole universe; sector_return/sector_beta aggregate each GICS sector's mean return
# over every symbol in it. Run per-shard they would see only ~1/8 of the universe and produce 8 different
# "market/sector-wide" values per minute, breaking live↔backfill parity — so they MUST run once in the
# reader's gather phase over every symbol.
REDUCE_GROUPS: tuple[str, ...] = (
    "cross_sectional_rank",
    "breadth",
    "market_turbulence",
    "sector_return",
    "sector_beta",
)
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
    to the full window for any reduce group that doesn't declare its depth (``reduce_buffer_minutes`` None).
    """
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
    the symbol set is fixed, so after the first minute this is a dict lookup, not an md5 per bar per minute.
    """
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


def route_ticks(ticks: list[dict], n_shards: int) -> list[list[dict]]:
    """Partition a minute's raw trade (or quote) dicts by ``hash(symbol) % n_shards`` — the SAME hash
    routing as the bars, so each symbol's ticks land on the worker that owns its bars and aggregates them
    locally. The index ETFs are REPLICATED to every shard (their bars already are), so each shard enriches
    its replicated SPY/QQQ/IWM bars with those symbols' ticks — exactly the single enriched index rows the
    reader produced and replicated before. ``ticks`` dicts are the reader's normalized shape with an ``S``
    symbol key (trades: S/p/s/ts_epoch; quotes: S/bp/ap/bs/as/ts_epoch)."""
    index_ticks = [tick for tick in ticks if tick["S"] in INDEX_SYMBOLS]
    routed: list[list[dict]] = [list(index_ticks) for _ in range(n_shards)]
    for tick in ticks:
        if tick["S"] in INDEX_SYMBOLS:
            continue  # already replicated to all shards
        routed[shard_of(tick["S"], n_shards)].append(tick)
    return routed


def unowned_index_symbols(shard_id: int, n_shards: int) -> frozenset[str]:
    """The index ETFs replicated into THIS shard for compute that it must NOT persist — every index symbol
    except the one this shard OWNS by the stable hash routing. Each index symbol is written by exactly one
    shard (its ``shard_of`` owner), so the store holds ONE (symbol, minute) row per broadcast symbol instead
    of N byte-identical copies (one per shard). The symbols still compute on every shard — only the persisted
    output is filtered, so feature values are unchanged (parity-neutral)."""
    return frozenset(symbol for symbol in INDEX_SYMBOLS if shard_of(symbol, n_shards) != shard_id)


def process_shard(
    state: CaptureState,
    bars: list[dict],
    root: str,
    mode: str,
    day: str | None,
    window: int,
    snapshots: dict | None = None,
    write: bool = True,
    shard: int | None = None,
    accumulate: bool = False,
    extra_frames: dict | None = None,
    drop_output_symbols: frozenset[str] = frozenset(),
) -> None:
    """One shard's map step: the shared core, minus the universe-wide reduce groups. Each minute appends
    its OWN per-minute file inside the partition (atomic, no clobber) so all shards write concurrently.

    ``extra_frames`` carries THIS minute's raw ``trades`` frame so the raw-trades groups
    (tick_runlength / microstructure_burst) run on the shard's own trades — built by ``aggregate_shard_ticks``.

    ``drop_output_symbols`` are the replicated index ETFs this shard does NOT own — computed here (required
    market-context inputs) but PERSISTED only by their owning shard, so each broadcast (symbol, minute) is
    written once. Pass ``unowned_index_symbols(shard, n_shards)``."""
    process_bars(
        state,
        bars,
        root,
        mode,
        day,
        window,
        snapshots,
        exclude_groups=REDUCE_GROUPS,
        write=write,
        shard=shard,
        accumulate=accumulate,
        extra_frames=extra_frames,
        drop_output_symbols=drop_output_symbols,
    )


def aggregate_shard_ticks(
    bars: list[dict],
    trades: list[dict],
    quotes: list[dict],
    minute_epoch: int,
    tick_states: dict[str, TickState],
) -> tuple[list[dict], pl.DataFrame]:
    """One shard's per-minute tick aggregation — moved OFF the single reader onto the worker that owns the
    shard, so the firehose is distributed across workers instead of all aggregated inline by the reader.

    Buckets THIS shard's raw trade/quote dicts to ``minute_epoch`` (the exchange-ts minute the backfill
    agrees with), aggregates per symbol with the threaded ``TickState`` (live == batch at the tick layer),
    and returns:
      * the bars ENRICHED with the minute_agg tick columns (n_trades / signed_volume / mean_spread_bps /
        ...) the trade_flow / quote_spread / liquidity groups consume — same as the reader produced before,
        now per shard; and
      * the raw ``trades`` frame (symbol, ts, price, size) the tick_runlength / microstructure_burst groups
        consume — which the reader NEVER built, so those two raw-trades groups produced nothing before.

    ``tick_states`` is the WORKER's per-symbol state, threaded across minutes — each symbol lives on exactly
    one shard (stable hash routing), so its tick history is contiguous on its owning worker and the live
    classification matches a single batch pass over that symbol's whole ordered tape (parity)."""
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
                QuoteTick(
                    ts_epoch=quote["ts_epoch"],
                    bid=quote["bp"],
                    ask=quote["ap"],
                    bid_size=quote["bs"],
                    ask_size=quote["as"],
                )
            )
    enriched = enrich_bars_with_ticks(bars, dict(trades_by_symbol), dict(quotes_by_symbol), tick_states)
    return enriched, trades_frame(dict(trades_by_symbol))


def process_reduce(
    reduce_state: CaptureState,
    bars: list[dict],
    root: str,
    mode: str,
    day: str | None,
    window: int,
    snapshots: dict | None = None,
    write: bool = True,
    accumulate: bool = False,
) -> None:
    """The gather step: compute the universe-wide reduce groups over ALL symbols once. The reader holds a
    MINIMAL full-universe buffer — projected to the columns the reduce groups read (close+volume + keys)
    and capped at the reduce groups' deepest declared window + slack, NOT the full 300m — and runs ONLY
    the reduce groups on it. Both the projection and the depth cap are derived from the reduce groups'
    declarations (``reduce_buffer_columns``/``reduce_buffer_minutes``) and are parity-neutral: the dropped
    columns and older minutes were never read on this path.

    ``snapshots`` carries the slowly-changing reference frames some reduce groups read — breadth needs the
    WHOLE-UNIVERSE ``reference`` (sector) and ``daily`` (close) frames to self-select (``runnable``) and to
    bucket sectors / compute its 1d/5d horizons. The reader passes its full (un-sharded) snapshots so the
    gather sees every symbol, exactly as the single-process path does."""
    process_bars(
        reduce_state,
        bars,
        root,
        mode,
        day,
        window,
        snapshots=snapshots,
        only_groups=REDUCE_GROUPS,
        write=write,
        accumulate=accumulate,
        project_columns=reduce_buffer_columns(),
        buffer_minutes=reduce_buffer_minutes(window),
    )


# The tail window (seconds before the minute close T+60) the at-bar tail-fold re-reads; the pre-pass owns
# everything before it. ``FP_SPECULATIVE_TAIL_S`` overrides (default 1s, matching the #378 prototype). The
# split is by EXCHANGE ts, so it is parity-neutral regardless of value — any cut reconstructs the same
# combined ordered tape (see ``speculative.tail_fold_aggregate``); the value only governs how much partition
# work lands off-path vs on-path.
SPECULATIVE_TAIL_SECONDS: float = float(os.environ.get("FP_SPECULATIVE_TAIL_S", "1"))


def _split_partial_tail(
    ticks: list[dict], minute_epoch: int, tail_seconds: float
) -> tuple[list[dict], list[dict]]:
    """Split a minute's reader tick dicts into (partial, tail) at ``T+60 − tail_seconds`` by EXCHANGE ts.
    ``partial`` = the early ticks the pre-pass partitions off the critical path; ``tail`` = the last-ε ticks
    the at-bar tail-fold re-reads. Off-minute ticks stay in ``partial`` (the pre-pass drops them by bucket,
    exactly as the non-speculative path does) so the union ``partial + tail`` is the whole received set."""
    cutoff = minute_epoch + 60.0 - tail_seconds
    partial: list[dict] = []
    tail: list[dict] = []
    for tick in ticks:
        if bucket_minute(tick["ts_epoch"]) == minute_epoch and tick["ts_epoch"] >= cutoff:
            tail.append(tick)
        else:
            partial.append(tick)
    return partial, tail


def speculative_aggregate_shard_ticks(
    bars: list[dict],
    trades: list[dict],
    quotes: list[dict],
    minute_epoch: int,
    tick_states: dict[str, TickState],
    tail_seconds: float = SPECULATIVE_TAIL_SECONDS,
    prepass_out: list[SpeculativeMinuteState] | None = None,
) -> tuple[list[dict], pl.DataFrame]:
    """The two-phase (pre-pass + tail-fold) equivalent of ``aggregate_shard_ticks`` — value-identical, with
    the bulk per-symbol partition moved OFF the critical path. Splits the minute's ticks at the ε boundary,
    runs the pre-pass over the partial set (the off-path installment) and the tail-fold over the last-ε
    (the on-path installment), and returns the IDENTICAL ``(enriched_bars, trades_frame)`` tuple.

    In the live worker the pre-pass would be triggered at wall-clock ~T−ε from reader-forwarded partial ticks
    (the activation step documented for the Lead in ``docs/SPECULATIVE_PRECOMPUTE.md``); here both installments
    run in-line at the bar so the mechanism is exercised value-identically on the real worker path (and so the
    flag is byte-identical-when-off, value-identical-when-on). ``prepass_out``, when given, captures the
    pre-pass state for the latency split-measurement — production passes ``None``."""
    partial_trades, tail_trades = _split_partial_tail(trades, minute_epoch, tail_seconds)
    partial_quotes, tail_quotes = _split_partial_tail(quotes, minute_epoch, tail_seconds)
    spec_state = prepass_aggregate(partial_trades, partial_quotes, minute_epoch)
    if prepass_out is not None:
        prepass_out.append(spec_state)
    return tail_fold_aggregate(spec_state, bars, tail_trades, tail_quotes, tick_states)


def worker_main(
    shard_id: int,
    n_shards: int,
    queue,
    root: str,
    mode: str,
    window: int,
    day: str | None,
    snapshots: dict | None,
    symbols: list[str] | None = None,
) -> None:  # pragma: no cover (process entry)
    """Persistent worker process entry: own ``shard_id``, drain the queue of minute bar-batches (already
    routed to this shard), and run the map step. A ``None`` batch is the shutdown sentinel.

    ``symbols`` are this shard's owned tickers (+ the replicated index ETFs); when ``FP_WARM_START=1`` and a
    session ``day`` is set, the shard's trailing ring is rehydrated from those symbols' already-settled bars
    BEFORE the first live minute, so a restart does not start cold (CRITICAL-2). Default OFF / no symbols =
    today's cold start, unchanged."""
    state = CaptureState()
    # Per-symbol tick state, threaded across minutes ON THIS WORKER. Each symbol lives on exactly one shard
    # (stable hash routing), so its trade tape is contiguous here and the live sign-classification matches a
    # single batch pass over that symbol's whole tape — the parity guarantee at the tick layer, now per shard.
    tick_states: dict[str, TickState] = {}
    if os.environ.get("FP_ASYNC_WRITE"):  # opt-in: a background writer thread (can contend with compute)
        state.writer = StoreWriter()
    if warm_start_enabled() and day and symbols:
        from quantlib.features.backfill_bars import backfill_bars

        # Alpaca historical RAW = the same unadjusted SIP bars this shard will stream — a parity-true seed.
        bars = backfill_bars(day, symbols)
        seeded = warm_start_ring(state, bars, depth=window)
        print(
            f"[worker {shard_id}] warm-started ring: {seeded} minutes from {bars.height} bars",
            file=sys.stderr,
            flush=True,
        )
    metrics.start_metrics_server(WORKER_METRICS_BASE_PORT + shard_id)  # /metrics for Prometheus/Grafana
    bench_log = _bench_log_path(root, shard_id)  # set FP_BENCH_LOG=1 to record per-minute shard latency
    if bench_log is not None:
        print(f"[worker {shard_id}] up", file=sys.stderr, flush=True)
    processed = 0
    # The replicated index ETFs this shard must compute but NOT persist — only their owning shard writes
    # them, so the store holds one (symbol, minute) row per broadcast symbol, not one per shard.
    drop_output_symbols = unowned_index_symbols(shard_id, n_shards)
    while True:
        item = queue.get()
        if item is None:
            if state.writer is not None:
                state.writer.flush()  # drain pending writes before exit so nothing is lost
                state.writer.stop()
            if bench_log is not None:
                print(f"[worker {shard_id}] exiting after {processed} minutes", file=sys.stderr, flush=True)
            return
        # Reader hands (first_arrival, last_arrival, symbol_arrivals, minute, bars, trades, quotes). All
        # arrival stamps are time.time() (cross-process comparable; perf_counter is not): first_arrival =
        # the minute's FIRST bar landing (end-to-end anchor, incl. Alpaca delivery spread), last_arrival =
        # the minute's LAST bar landing (pure-compute anchor), symbol_arrivals = per-symbol arrival for the
        # drill-down. ``trades``/``quotes`` are THIS shard's raw ticks (reader forwards them un-aggregated so
        # the firehose distributes across workers); empty when no ticks are subscribed.
        first_arrival, last_arrival, symbol_arrivals, minute, batch, trades, quotes = item
        processed += 1
        start = time.perf_counter()
        # Aggregate THIS shard's ticks on THIS worker: enrich the bars with the minute_agg tick columns and
        # build the raw ``trades`` frame the tick_runlength / microstructure_burst groups consume. No
        # subscribed ticks -> empty trades/quotes -> bars pass through unenriched, trades frame empty.
        if trades or quotes:
            minute_epoch = bucket_minute(minute.timestamp())
            # FP_SPECULATIVE flips the per-minute tick aggregation onto the two-phase pre-pass + tail-fold
            # schedule (value-identical, partition work moved off the critical path). DEFAULT OFF -> the
            # unchanged one-installment path. The output buffer is byte-identical either way.
            if speculative_enabled():
                batch, trades_df = speculative_aggregate_shard_ticks(
                    batch, trades, quotes, minute_epoch, tick_states
                )
            else:
                batch, trades_df = aggregate_shard_ticks(batch, trades, quotes, minute_epoch, tick_states)
            extra_frames = {"trades": trades_df} if trades_df.height else None
        else:
            extra_frames = None
        process_shard(
            state,
            batch,
            root,
            mode,
            day,
            window,
            snapshots,
            shard=shard_id,
            extra_frames=extra_frames,
            drop_output_symbols=drop_output_symbols,
        )
        ready_wallclock = time.time()
        # Two complementary latencies, both ending at the assemble (the bet point) with the post-bet
        # parquet write subtracted (process_bars records last_write_ms separately; subtract it whether the
        # write was sync here or async-queued). feature_vector_latency_seconds (first-bar) is end-to-end
        # incl. Alpaca's delivery spread; feature_assemble_seconds (last-bar) is OUR pure compute.
        write_seconds = state.last_write_ms / 1000.0
        metrics.record_bar_to_vector(shard_id, max(0.0, ready_wallclock - first_arrival - write_seconds))
        metrics.record_assemble(shard_id, max(0.0, ready_wallclock - last_arrival - write_seconds))
        # Dispatch-INDEPENDENT views (never saturate, unlike the two above which are gated on the reader's
        # next-minute-bar dispatch trigger). feed_delivery = provider lag from the minute's CLOSE (boundary +
        # 60s) to its first bar landing; shard_compute = pure worker compute from queue-pickup (``start``)
        # to assemble with the post-bet write subtracted.
        minute_boundary_epoch = minute.timestamp()
        bar_close_epoch = minute_boundary_epoch + 60.0
        metrics.record_feed_delivery(shard_id, max(0.0, first_arrival - bar_close_epoch))
        metrics.record_shard_compute(shard_id, max(0.0, (time.perf_counter() - start) - write_seconds))
        # Drill-down (best-effort, fault-isolated, off the hot path): top-K slowest symbols this minute ->
        # latency_slow_symbols. A DB error logs a warning and continues — it must never stall capture.
        slow_rows = latency_drilldown.top_k_slow_symbols(
            symbol_arrivals, ready_wallclock, minute_boundary_epoch
        )
        latency_drilldown.write_slow_symbols(minute, shard_id, slow_rows)
        if bench_log is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            # The bet-relevant latency is COMPUTE only; the write happens after the decision, so report it
            # separately and subtract it from the critical-path "ms".
            record = {
                "shard": shard_id,
                "minute": max(bar["t"] for bar in batch),
                "ms": elapsed_ms - state.last_write_ms,
                "write_ms": state.last_write_ms,
                "groups": dict(state.group_timings),
            }
            with bench_log.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
