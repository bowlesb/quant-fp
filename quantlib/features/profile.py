"""Per-group compute-latency profiler — first-class timing so every feature's cost is visible.

"Time the hell out of every feature." Each FeatureGroup is the vectorized compute unit (one pass
emits all its features), so the natural timing granularity is per group, with per-feature cost
derived. This surfaces a latency table sorted by cost plus a projection to a target ticker scale, so
a newly-added group that is slow is caught immediately — the standing rule is that a feature earns
its place only if it is timed and fast. Backs both a CLI and (later) a latency API endpoint.

Usage: python -m quantlib.features.profile [n_tickers] [window_min] [daily_days] [reps]
"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.reduction_anchor import attach_volume_anchor
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.engine import run_group
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.stateful import StatefulEngine, StatefulGroup

BASE = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)

# The TRUE live raw-tape breadth: only the tick-SUBSCRIBED symbols stream a raw trades feed (the live
# default is the liquid canary set ``real_capture.DEFAULT_TICK_SYMBOLS``; ops widen it with FP_TICK_SYMBOLS).
# A group that reads the ``trades`` frame therefore sees ticks for only this many symbols live — NOT the
# full minute-bar universe. Profiling such a group over a full-universe synthetic tape over-states its live
# cost by the breadth ratio (the #381 §2d profiler artifact), so the LIVE timing thins the tape to this
# breadth. Bar/minute-agg groups are unaffected (they read every symbol's bar) and keep the full universe.
# Kept as a literal so the profiler (+ its pytest budget gate) imports with NO DB/capture dependency;
# ``test_fp_latency_budget`` asserts it stays equal to ``len(DEFAULT_TICK_SYMBOLS)``.
LIVE_TICK_BREADTH = 24
INTRADAY_COLS = ("open", "close", "high", "low", "volume", "n_trades", "signed_volume",
                 "mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size")


def build_frames(
    n_tickers: int, window_min: int, daily_days: int, include_trades: bool = True
) -> dict[str, pl.DataFrame]:
    """Synthetic but schema-faithful frames at a target scale (intraday buffer + daily cache +
    reference snapshot), so the profiler exercises every runnable group. ``include_trades`` adds a raw
    tape so the trades-frame groups are runnable too (default on — the profiler and the latest-minute
    parity test need it); pass False to profile ONLY the minute-bar path (the latency ceiling gate, whose
    ``us_per_feature`` ceiling is calibrated for bar groups, not the few-feature sub-minute tape groups)."""
    symbols = pl.DataFrame({"symbol": [f"S{i}" for i in range(n_tickers)]})
    minutes = pl.DataFrame({"minute": [BASE + timedelta(minutes=j) for j in range(window_min)]})
    intraday = symbols.join(minutes, how="cross").with_columns(
        [(100.0 + (pl.int_range(pl.len()) % 97) * 0.1).alias(c) for c in INTRADAY_COLS]
    )
    days = pl.DataFrame({"date": [BASE + timedelta(days=j) for j in range(daily_days)]})
    daily = symbols.join(days, how="cross").with_columns(
        [pl.col("date").dt.date()]
        + [(100.0 + (pl.int_range(pl.len()) % 250) * 0.2).alias(c) for c in ("open", "high", "low", "close", "vwap")]
        + [(1e6 + (pl.int_range(pl.len()) % 500) * 1e3).alias("volume")]
    )
    reference = symbols.with_columns(
        [pl.lit("Technology").alias("sector"), pl.lit(True).alias("shortable"),
         pl.lit(True).alias("easy_to_borrow"), pl.lit(True).alias("marginable"), pl.lit(False).alias("fractionable")]
    )
    # Synthetic EDGAR filings snapshot: a handful per symbol spread across the trailing year (one inside
    # the session window so the same-session look-ahead gate is exercised) so the edgar_filing_frequency
    # group is runnable in the profiler + the generic latest-parity test.
    filing_forms = ("8-K", "10-Q", "10-K", "4", "SC 13G")
    filings = symbols.join(
        pl.DataFrame({"_off": list(range(8))}), how="cross"
    ).with_columns(
        pl.col("_off").map_elements(lambda i: filing_forms[i % len(filing_forms)], return_dtype=pl.String).alias("form_type"),
        (BASE - pl.duration(days=pl.col("_off") * 45) + pl.duration(minutes=pl.col("_off"))).alias("available_at"),
    ).select("symbol", "form_type", "available_at")
    # Synthetic news snapshot: a few sentiment-scored articles per symbol over the trailing week (one inside
    # the session window so the same-session look-ahead gate is exercised) so the news_sentiment group is
    # runnable in the profiler + the generic latest-parity test.
    news = symbols.join(
        pl.DataFrame({"_off": list(range(6))}), how="cross"
    ).with_columns(
        (BASE - pl.duration(days=pl.col("_off")) + pl.duration(minutes=pl.col("_off"))).alias("available_at"),
        (((pl.int_range(pl.len()) % 5) - 2) / 2.0).cast(pl.Float64).alias("sentiment"),
    ).select("symbol", "available_at", "sentiment")
    # Attach the per-symbol volume centering anchor (from the daily snapshot) to minute_agg — the same
    # attachment production capture / backfill apply where minute_agg is built, so the centered-std groups
    # (volume) are runnable and center identically here.
    intraday = attach_volume_anchor(intraday, daily)
    frames = {"minute_agg": intraday, "daily": daily, "reference": reference, "filings": filings, "news": news}
    if include_trades:
        frames["trades"] = _build_trades(symbols, window_min)
    return frames


_TRADES_PER_MINUTE = 12  # synthetic tape density per (symbol, minute) — enough ticks to exercise the burst groups


def _build_trades(symbols: pl.DataFrame, window_min: int) -> pl.DataFrame:
    """Schema-faithful raw tape (symbol, ts, price, size): ``_TRADES_PER_MINUTE`` prints spread across each
    minute of the buffer, so the trades-frame groups (own-minute + windowed) are runnable and the generic
    latest-minute parity test exercises their ``compute_latest`` slice path against the rolling ``compute()``."""
    minutes = pl.DataFrame({"_min": [BASE + timedelta(minutes=j) for j in range(window_min)]})
    ticks = pl.DataFrame({"_k": list(range(_TRADES_PER_MINUTE))})
    tape = symbols.join(minutes, how="cross").join(ticks, how="cross")
    idx = pl.int_range(pl.len())
    return tape.select(
        pl.col("symbol"),
        # spread ticks across the minute (5s apart) so within-minute timing/gap features are non-degenerate
        (pl.col("_min") + pl.duration(seconds=pl.col("_k") * 5)).alias("ts"),
        (100.0 + (idx % 53) * 0.01).alias("price"),
        (100.0 + (idx % 37) * 25.0).alias("size"),
    )


def reads_raw_trades(group: FeatureGroup) -> bool:
    """True iff the group consumes the raw ``trades`` tape (its declared inputs name it). These are the
    hand-written sub-minute tick groups; live they see ticks for only the tick-SUBSCRIBED symbols
    (``LIVE_TICK_BREADTH``), so their live cost is measured on a tape thinned to that breadth."""
    return any(spec.name == "trades" for spec in group.inputs)


def runs_incremental(group: FeatureGroup) -> bool:
    """True iff the group rides the INCREMENTAL running-sum path live (the default since #391): an
    ``incremental_safe`` ``ReductionGroup``. Live capture seeds the per-shard ``IncrementalEngine`` once and
    folds O(1) per minute via ``step`` for exactly these groups; the conditioning-sensitive ones
    (``incremental_safe=False`` — price_volume/market_beta/residual_analysis...) stay on the batch fresh-sum
    recompute and so keep their batch ``compute_latest`` cost. Mirrors the live ``capture.py`` split."""
    return isinstance(group, ReductionGroup) and group.incremental_safe


def _incremental_step_call(group: ReductionGroup, frames: dict[str, pl.DataFrame]) -> Callable[[], object]:
    """A callable timing one armed ``ReductionGroup``'s LIVE incremental per-minute cost: build a single-group
    ``IncrementalEngine``, ``seed`` it once over the buffer (the warm-up the live session pays once, NOT a
    per-minute cost), then return ``step`` over the same buffer's latest minute — the O(1) fold + assemble the
    live capture actually runs each tick (the SAME ``assemble_from_long`` core as the batch). Timed in
    ISOLATION (one group per engine) to stay rankable against the other per-group rows, exactly as the batch
    rows are timed standalone; the over-count caveat (a group's true in-flow cost is its share of the SHARED
    engine's fold, not a standalone fold) is documented in the JSON header."""
    engine = IncrementalEngine([group])
    buffer_frame = frames[group.inputs[0].name]
    engine.seed(buffer_frame)
    return lambda: engine.step(buffer_frame)


def thin_trades_to_live_breadth(
    frames: dict[str, pl.DataFrame], n_syms: int = LIVE_TICK_BREADTH
) -> dict[str, pl.DataFrame]:
    """A copy of ``frames`` with the ``trades`` tape restricted to ``n_syms`` symbols — the live raw-tape
    breadth a tick-group actually sees. The bar/daily/reference frames are untouched (every symbol streams
    a bar). No ``trades`` frame -> returned unchanged."""
    if "trades" not in frames:
        return frames
    keep = {f"S{i}" for i in range(n_syms)}
    thinned = dict(frames)
    thinned["trades"] = frames["trades"].filter(pl.col("symbol").is_in(keep))
    return thinned


def _measure(call: Callable[[], object], reps: int) -> float:
    """Min wall-clock ms over ``reps`` runs of ``call`` after one warmup."""
    call()  # warmup (JIT/import/cache priming excluded)
    times = []
    for _ in range(reps):
        start = time.perf_counter()
        call()
        times.append(time.perf_counter() - start)
    return min(times) * 1000.0


def _live_call(group: FeatureGroup, frames: dict[str, pl.DataFrame]) -> Callable[[], object]:
    """The callable that exercises a group's TRUE live per-minute path (#381 measurement-honesty):

      * a ``StatefulGroup`` -> seed its ``StatefulEngine`` once from the buffer (the warm-up, not a per-minute
        cost) then time ``step()`` — the O(1) fold the live capture actually runs, NOT the rolling-derive
        ``compute_latest`` backfill twin (``_state_frame_rolling`` rebuilds whole-buffer EMAs/joins each call);
      * an ``incremental_safe`` ``ReductionGroup`` (the 15 armed since #391) -> seed an ``IncrementalEngine``
        once then time ``step()`` — the O(1) running-sum fold the live capture folds each minute, NOT the
        whole-buffer ``compute_latest`` batch recompute (which the profiler used to time, so the dashboard
        showed the BATCH cost even though the live fc runs the incremental fast path);
      * a raw-``trades`` tick group -> ``compute_latest`` over a tape thinned to ``LIVE_TICK_BREADTH`` symbols;
      * everything else -> ``compute_latest`` over the full frames (already the live path)."""
    if isinstance(group, StatefulGroup):
        engine = StatefulEngine(group)
        buffer_frame = frames[group.inputs[0].name]
        engine.seed(buffer_frame)
        ctx = BatchContext(frames=frames)
        # A HYBRID stateful group (e.g. technical, with windowed reduction columns) needs ctx so step() can
        # join them; a pure recursive/lag group declares none and folds with ctx=None.
        hybrid_ctx = ctx if group.reduction_columns(ctx) is not None else None
        return lambda: engine.step(buffer_frame, hybrid_ctx)
    if runs_incremental(group):
        return _incremental_step_call(group, frames)
    if reads_raw_trades(group):
        ctx = BatchContext(frames=thin_trades_to_live_breadth(frames))
        return lambda: group.compute_latest(ctx)
    ctx = BatchContext(frames=frames)
    return lambda: group.compute_latest(ctx)


def time_group(group: FeatureGroup, frames: dict[str, pl.DataFrame], reps: int = 3, latest: bool = False) -> float:
    """Min wall-clock ms over ``reps`` runs (after a warmup) of one group's compute. ``latest=True`` times
    the TRUE LIVE per-minute path (what the per-minute budget actually pays): ``StatefulEngine.step()`` for
    a stateful group and a live-breadth-thinned tape for a raw-trades tick group (#381), ``compute_latest``
    otherwise; ``latest=False`` times the backfill ``compute()``."""
    if latest:
        return _measure(_live_call(group, frames), reps)
    ctx = BatchContext(frames=frames)
    return _measure(lambda: run_group(group, ctx, validate=False), reps)


def profile(frames: dict[str, pl.DataFrame], reps: int = 3, latest: bool = False) -> pl.DataFrame:
    """Latency table for every runnable group, sorted slowest-first. ``latest`` times the live path."""
    rows = []
    for group in runnable(frames):
        ms = time_group(group, frames, reps, latest=latest)
        n_features = len(group.feature_names)
        rows.append(
            {"group": group.name, "type": group.type.value, "n_features": n_features,
             "ms": round(ms, 1), "us_per_feature": round(ms * 1000.0 / n_features, 1)}
        )
    return pl.DataFrame(rows).sort("ms", descending=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    latest = "--latest" in sys.argv  # time compute_latest (live path) instead of compute() (backfill)
    n_tickers = int(args[0]) if len(args) > 0 else 2000
    window_min = int(args[1]) if len(args) > 1 else 120
    daily_days = int(args[2]) if len(args) > 2 else 250
    reps = int(args[3]) if len(args) > 3 else 5
    frames = build_frames(n_tickers, window_min, daily_days)
    table = profile(frames, reps, latest=latest)
    total_ms = table["ms"].sum()
    total_feats = int(table["n_features"].sum())
    pl.Config.set_tbl_rows(100)
    path = "LIVE (compute_latest)" if latest else "BACKFILL (compute)"
    print(f"=== {path} per-group latency @ {n_tickers} tickers x {window_min}m buffer ({reps} reps, min) ===")
    print(table)
    print(f"\nTOTAL: {total_feats} features across {table.height} groups in {total_ms:.0f} ms "
          f"({1000.0 * total_ms / total_feats:.1f} us/feature) at {n_tickers} tickers")
    print(f"slowest group: {table.row(0, named=True)['group']} ({table.row(0, named=True)['ms']} ms)")


if __name__ == "__main__":
    main()
