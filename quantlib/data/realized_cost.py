"""Realized per-name half-spread, measured DIRECTLY from the raw NBBO quote tape (no model).

Stage 1 of the cost-accuracy work (the G0b finding): the harness backtest cost term was a FLAT
``DEFAULT_HALF_SPREAD_BPS = 3.0`` stub for every name. The quote-tape G0 screen proved that stub
UNDERCHARGES the true realized half-spread by ~2.6x on average (realized mean ~7.9 bps, median ~6.5,
p10-p90 ~1.9-15.6) and captures none of the 4-8x per-name variation — so every net-of-cost $-verdict was
optimistic. This module replaces the stub with the MEASURED truth.

For a name at a decision instant T, the cost it pays to cross the spread is the half-spread quoted around
T. We measure the TIME-WEIGHTED mean relative half-spread over a short trailing window ``[T - window, T)``
(time-weighted because a quote that stood 5s is 5x as representative of the executable price as a 1ms
flicker). This is MEASURED, not predicted — it reads the actual book, so it is unimpeachable truth for a
backtest (the entry instant's realized cost is known ex-post). The PREDICTED model for live/forward use
(where realized cost is unknown at decision time) is Stage 2, pre-registered separately.

Quote validity + the spread formula match ``raw_loaders._tick_minute_columns`` exactly (``(ask-bid)/mid``
in bps, guarded by ``mid>0 & ask>=bid`` and positive sizes) so this is the SAME spread the platform's
quote features measure — only time-weighted at the entry instant rather than count-averaged per minute.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import numpy as np
import polars as pl

from quantlib.data.raw_backfill import partition_dir

logger = logging.getLogger("realized_cost")

DEFAULT_COST_WINDOW_MIN: int = 5  # trailing minutes over which the entry half-spread is measured
MIN_QUOTES: int = 5  # too few valid quotes in the window -> unreliable, return NaN (caller falls back)


def _read_quote_window(
    store: str, symbol: str, day: dt.date, lo: dt.datetime, hi: dt.datetime
) -> pl.DataFrame | None:
    """Valid-NBBO quotes for one symbol in ``[lo, hi)`` (quote-staleness-safe: strict ``ts < hi``)."""
    path = os.path.join(partition_dir(store, "quotes", symbol, day), "data.parquet")
    if not os.path.exists(path):
        return None
    frame = (
        pl.read_parquet(path, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
        .filter((pl.col("ts") >= lo) & (pl.col("ts") < hi))
        .filter(
            (pl.col("bid_price") > 0)
            & (pl.col("ask_price") > pl.col("bid_price"))
            & (pl.col("bid_size") > 0)
            & (pl.col("ask_size") > 0)
        )
        .sort("ts")
    )
    return frame if frame.height >= MIN_QUOTES else None


def _time_weighted_half_spread_bps(quotes: pl.DataFrame, end: dt.datetime) -> float:
    """Time-weighted mean relative HALF-spread (bps) over the window, last quote's dwell -> ``end``."""
    mid = (pl.col("ask_price") + pl.col("bid_price")) / 2.0
    spread = quotes.with_columns(((pl.col("ask_price") - pl.col("bid_price")) / mid * 10000.0).alias("_sp"))
    timestamps = spread["ts"].to_numpy()
    end_np = np.datetime64(end.replace(tzinfo=None), "us")
    next_ts = np.append(timestamps[1:], end_np)
    dwell_seconds = (next_ts - timestamps) / np.timedelta64(1, "s")
    spread_bps = spread["_sp"].to_numpy()
    weight_sum = float(np.sum(dwell_seconds))
    if weight_sum <= 1e-9:
        return float(np.mean(spread_bps)) / 2.0
    return float(np.sum(spread_bps * dwell_seconds) / weight_sum) / 2.0


def realized_half_spread_bps(
    store: str,
    day: str,
    symbols: list[str],
    at_ts: dt.datetime,
    *,
    window_min: int = DEFAULT_COST_WINDOW_MIN,
) -> pl.DataFrame:
    """Measured per-name realized one-way half-spread (bps) at the entry instant ``at_ts``, from the quote
    tape over ``[at_ts - window_min, at_ts)``. ``store`` is the store ROOT (e.g. ``/store``; the ``raw/
    quotes/...`` suffix is added internally). Returns a ``(symbol, realized_half_spread_bps)`` frame; a
    name with too few valid quotes is omitted (the caller falls back to the flat stub for it)."""
    target = dt.date.fromisoformat(day)
    lo = at_ts - dt.timedelta(minutes=window_min)
    rows: list[dict[str, str | float]] = []
    for symbol in symbols:
        quotes = _read_quote_window(store, symbol, target, lo, at_ts)
        if quotes is None:
            continue
        rows.append(
            {"symbol": symbol, "realized_half_spread_bps": _time_weighted_half_spread_bps(quotes, at_ts)}
        )
    if not rows:
        logger.warning(
            f"no realized half-spread measurable for {day} at {at_ts.isoformat()} ({len(symbols)} syms)"
        )
        return pl.DataFrame(schema={"symbol": pl.Utf8, "realized_half_spread_bps": pl.Float64})
    return pl.DataFrame(rows)
