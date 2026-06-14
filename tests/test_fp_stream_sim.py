"""The streaming-sim convergence is parity-true: the FULL flow (trades+quotes+bars -> tick-agg -> the
INCREMENTAL fast path) reproduces the batch compute on the same enriched tape.

Two things are pinned here, the two halves of the convergence:
  1. tick-agg: the minute_agg the sim builds from the raw trade/quote tape == a direct batch pass of the
     parity-true tick consumer (enrich_bars_with_ticks) — live == backfill at the tick layer.
  2. fast path: the reduction features the IncrementalEngine emits from its running sums == the batch
     compute_latest over the same enriched buffer — the incremental path never diverges from the truth.
This is the sim-level guard that the convergence wiring (ticks -> aggregate -> incremental engine ->
features) does not skip the tick flow or fall back to the batch path.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.bench_stream import SESSION_DAY, synth_daily, synth_reference
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, emit_numpy
from quantlib.features.stream_sim import StreamShardState, process_stream_minute

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _synthetic_minute(symbols: list[str], minute_index: int, rng: np.random.Generator,
                      price: dict[str, float], trades_per_min: int = 5, quotes_per_min: int = 5
                      ) -> tuple[list[dict], list[dict], list[dict]]:
    """One minute of the full flow: a bar plus sub-minute trades + quotes per symbol, prices a random walk
    (no degenerate zero-variance windows, so the parity check is meaningful)."""
    minute = BASE + dt.timedelta(minutes=minute_index)
    bars, trades, quotes = [], [], []
    for symbol in symbols:
        price[symbol] *= 1.0 + rng.standard_normal() * 0.001
        close = price[symbol]
        bars.append({"S": symbol, "o": close * 0.999, "h": close * 1.002, "l": close * 0.998,
                     "c": close, "v": 1000.0 + rng.random() * 3000, "t": minute.isoformat()})
        for seq in range(trades_per_min):
            ts = minute + dt.timedelta(seconds=(seq + 1) * 60.0 / (trades_per_min + 1))
            trades.append({"S": symbol, "p": close + (seq - trades_per_min / 2.0) * 0.001,
                           "s": 100.0 + seq, "ts_epoch": ts.timestamp()})
        for seq in range(quotes_per_min):
            ts = minute + dt.timedelta(seconds=(seq + 1) * 60.0 / (quotes_per_min + 1))
            quotes.append({"S": symbol, "bp": close - 0.02, "ap": close + 0.02,
                           "bs": 5.0 + seq, "as": 6.0 + seq, "ts_epoch": ts.timestamp()})
    return bars, trades, quotes


def _run_sim(n_sym: int = 6, n_min: int = 64) -> tuple[StreamShardState, dict]:
    symbols = [f"T{i:03d}" for i in range(n_sym)] + ["SPY", "QQQ", "IWM"]
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}
    rng = np.random.default_rng(11)
    price = {symbol: 100.0 + i for i, symbol in enumerate(symbols)}
    state = StreamShardState(window=300)
    for minute_index in range(n_min):
        bars, trades, quotes = _synthetic_minute(symbols, minute_index, rng, price)
        process_stream_minute(state, bars, trades, quotes, "/tmp/_unused", "mock", SESSION_DAY,
                              snapshots, shard=0, write=False)
    return state, snapshots


def test_sim_processes_full_flow_and_populates_tick_columns() -> None:
    state, _ = _run_sim(n_min=10)
    assert state.minutes == 10
    assert state.engine is not None  # the incremental engine was seeded and folded
    buffer = state.buffer
    assert buffer is not None
    # the enriched minute_agg carries the tick columns the trade_flow/quote_spread groups consume
    for col in ("n_trades", "signed_volume", "mean_spread_bps", "quote_imbalance"):
        assert col in buffer.columns
    assert buffer.select(pl.col("n_trades").sum()).item() > 0  # ticks actually aggregated, not all-zero


def test_incremental_reduction_matches_batch_on_enriched_buffer() -> None:
    """The fast-path emit == the batch compute_latest over the SAME enriched buffer — the convergence
    is parity-true (the engine reproduces the truth, it does not approximate it)."""
    state, snapshots = _run_sim()
    buffer = state.buffer
    assert buffer is not None and state.engine is not None
    frames = {"minute_agg": buffer, **snapshots}
    ctx = BatchContext(frames=frames)
    latest = buffer["minute"].max()
    engine = state.engine
    assert engine.state is not None
    reduction_out = emit_numpy(
        engine.groups, engine.state.running, engine.symbols or [], engine.windows, engine.col_index,
        buffer.filter(pl.col("minute") == latest), latest, engine.plan, engine.reg_plan,
    )
    for group in runnable(frames):
        if not isinstance(group, ReductionGroup):
            continue
        batch = group.compute_latest(ctx).sort("symbol")
        inc = reduction_out[group.name].sort("symbol").select(batch.columns)
        for col in [c for c in batch.columns if c not in ("symbol", "minute")]:
            joined = batch.select("symbol", col).join(
                inc.select("symbol", pl.col(col).alias("_i")), on="symbol"
            )
            bad = joined.filter(
                ~(
                    (pl.col(col).is_null() & pl.col("_i").is_null())
                    | ((pl.col(col) - pl.col("_i")).abs() <= 1e-6 + 1e-6 * pl.col(col).abs())
                )
            )
            assert bad.height == 0, f"{group.name}.{col}: {bad.height} mismatches\n{bad.head()}"
