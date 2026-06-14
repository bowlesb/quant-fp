"""Helper for the live LATEST-MINUTE form of windowed features.

A rolling reduction evaluated at the current minute T is just an aggregate over the trailing window
slice. ``slice_aggregates`` does that uniformly: for each window, slice the buffer to (T-w, T], group
by symbol, and apply the group's aggregations — one row per symbol. The group preps its per-minute
columns ONCE and every window reuses them (no duplicated per-minute work). Parity to the whole-buffer
rolling form is guaranteed generically by tests/test_fp_latest.py. At per-ticker-shard scale (~hundreds
of symbols per process) each group_by is tiny, and the shards run on all cores.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import polars as pl
import quant_tick


def slice_aggregates(
    frame: pl.DataFrame,
    windows: tuple[int, ...] | list[int],
    agg_for_window: Callable[[int], list[pl.Expr]],
    by: str = "minute",
) -> tuple[pl.DataFrame, dt.datetime]:
    """Per-window aggregate-at-T. ``frame`` must be sorted by (symbol, ``by``). ``agg_for_window(w)``
    returns the aggregation expressions for window ``w`` (the final named features, or intermediates the
    caller derives from). Returns (one row per symbol with the window aggregates, the latest minute T)."""
    latest = frame[by].max()
    out = frame.filter(pl.col(by) == latest).select("symbol")
    for w in windows:
        low = latest - dt.timedelta(minutes=w)
        agg = (
            frame.filter((pl.col(by) > low) & (pl.col(by) <= latest))
            .group_by("symbol")
            .agg(agg_for_window(w))
        )
        out = out.join(agg, on="symbol", how="left")
    return out, latest


def rust_reductions(frame: pl.DataFrame, value_col: str, windows: tuple[int, ...] | list[int], by: str = "minute") -> pl.DataFrame:
    """Move a windowed reduction's HEAVY COMPUTE to Rust — the drop-in alternative to the Polars rolling
    form. ``frame`` is sorted by (symbol, ``by``); returns a LONG frame (symbol, window, sum, mean, std,
    min, max) — one row per (symbol, window). Same numbers as the rolling form (the generic parity test
    guards it), fresh each minute (no drift). This is the WHOLE Python↔Rust seam: a group swaps its
    Polars rolling for a ``rust_reductions`` call; declare()/registry/parity are untouched."""
    long_schema = {"symbol": pl.String, "window": pl.Int64, "sum": pl.Float64, "mean": pl.Float64,
                   "std": pl.Float64, "min": pl.Float64, "max": pl.Float64}
    prepared = (
        frame.filter(pl.col(value_col).is_not_null())  # drop nulls -> kernel counts non-null, matching Polars rolling
        .with_columns(pl.col(by).dt.epoch("s").alias("_mi"))
        .sort(["symbol", "_mi"])
    )
    if prepared.height == 0:
        return pl.DataFrame(schema=long_schema)
    latest = frame[by].max()
    t = int(latest.timestamp())
    uniq = sorted(prepared["symbol"].unique().to_list())
    codes = pl.DataFrame({"symbol": uniq, "_c": list(range(len(uniq)))}, schema={"symbol": pl.String, "_c": pl.Int64})
    prepared = prepared.join(codes, on="symbol", how="left").sort(["_c", "_mi"])
    win_min = sorted(int(w) for w in windows)
    sym, win, n, total, sumsq, mn, mx = quant_tick.windowed_reduce(
        prepared["_c"].to_list(), prepared["_mi"].to_list(), prepared[value_col].to_list(),
        [w * 60 for w in win_min], t,
    )
    reverse = dict(enumerate(uniq))
    long = pl.DataFrame(
        {"symbol": [reverse[c] for c in sym], "window": [w // 60 for w in win],
         "_n": n, "sum": total, "_sumsq": sumsq, "min": mn, "max": mx}
    )
    return long.with_columns(
        [
            pl.when(pl.col("_n") > 0).then(pl.col("sum") / pl.col("_n")).otherwise(None).alias("mean"),
            pl.when(pl.col("_n") > 1)
            .then(((pl.col("_sumsq") - pl.col("sum") ** 2 / pl.col("_n")) / (pl.col("_n") - 1)).sqrt())
            .otherwise(None)
            .alias("std"),
        ]
    ).select(["symbol", "window", "sum", "mean", "std", "min", "max"])


def pivot_stat(long: pl.DataFrame, stat: str, name_fmt: str, windows: tuple[int, ...] | list[int]) -> pl.DataFrame:
    """Pivot one statistic from rust_reductions' long frame into named per-window feature columns
    (``name_fmt`` e.g. "realized_vol_{w}m"), one row per symbol. ALWAYS returns the full expected column
    set — any window absent (warmup / no data) is filled null — so the caller's select never breaks."""
    expected = [name_fmt.format(w=w) for w in windows]
    if long.height == 0:
        return pl.DataFrame(schema={"symbol": pl.String, **{c: pl.Float64 for c in expected}})
    wide = long.pivot(values=stat, index="symbol", on="window")
    wide = wide.rename({str(w): name_fmt.format(w=w) for w in windows if str(w) in wide.columns})
    missing = [c for c in expected if c not in wide.columns]
    if missing:
        wide = wide.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in missing])
    return wide.select(["symbol", *expected])
