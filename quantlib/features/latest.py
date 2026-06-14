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
        prepared["_c"].to_numpy(), prepared["_mi"].to_numpy(), prepared[value_col].to_numpy(),
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


def rust_windowed_sums(
    frame: pl.DataFrame, value_cols: list[str], windows: tuple[int, ...] | list[int], by: str = "minute"
) -> pl.DataFrame:
    """SINGLE-PASS windowed sums of MANY columns via the Rust kernel (quant_tick.windowed_sums) — the
    replacement for one-buffer-scan-per-window Polars slicing. ``frame`` sorted by (symbol, by). Nulls
    are summed as 0 (the caller passes a present-indicator column if it needs the non-null count).
    Returns a LONG frame (symbol, window, _n, <sum of each value_col under its own name>)."""
    latest = frame[by].max()
    t = int(latest.timestamp())
    uniq = sorted(frame["symbol"].unique().to_list())
    codes = pl.DataFrame({"symbol": uniq, "_c": list(range(len(uniq)))}, schema={"symbol": pl.String, "_c": pl.Int64})
    prepared = (
        frame.join(codes, on="symbol", how="left")
        .with_columns([pl.col(by).dt.epoch("s").alias("_mi")] + [pl.col(c).fill_null(0.0) for c in value_cols])
        .sort(["_c", "_mi"])
    )
    win_min = sorted(int(w) for w in windows)
    sym, win, n, sums = quant_tick.windowed_sums(
        prepared["_c"].to_numpy(), prepared["_mi"].to_numpy(),
        [prepared[c].to_numpy() for c in value_cols], [w * 60 for w in win_min], t,
    )
    reverse = dict(enumerate(uniq))
    data: dict[str, list] = {"symbol": [reverse[c] for c in sym], "window": [w // 60 for w in win], "_n": n}
    for idx, col in enumerate(value_cols):
        data[col] = sums[idx]
    return pl.DataFrame(data)


def windowed_ols_latest(
    frame: pl.DataFrame, x: str, y: str, windows: tuple[int, ...] | list[int], by: str = "minute"
) -> pl.DataFrame:
    """Aggregate-at-T OLS of ``y`` on ``x`` per window via the SINGLE-PASS Rust sums kernel, then the same
    slope/corr/r2/mean algebra as ``with_ols_columns`` — so the live OLS matches the rolling backfill form
    (parity-guarded). Pairs only where BOTH x,y are present. Returns LONG (symbol, window, slope, corr,
    r2, mean_y)."""
    both = pl.col(x).is_not_null() & pl.col(y).is_not_null()
    prepared = frame.with_columns(
        [
            pl.when(both).then(pl.col(x)).otherwise(0.0).alias("_olx"),
            pl.when(both).then(pl.col(y)).otherwise(0.0).alias("_oly"),
            both.cast(pl.Float64).alias("_olb"),
        ]
    )
    prepared = prepared.with_columns(
        [
            (pl.col("_olx") * pl.col("_oly")).alias("_olxy"),
            (pl.col("_olx") * pl.col("_olx")).alias("_olxx"),
            (pl.col("_oly") * pl.col("_oly")).alias("_olyy"),
        ]
    )
    long = rust_windowed_sums(prepared, ["_olb", "_olx", "_oly", "_olxy", "_olxx", "_olyy"], windows, by=by)
    n, sx, sy, sxy, sxx, syy = (pl.col(c) for c in ("_olb", "_olx", "_oly", "_olxy", "_olxx", "_olyy"))
    denom_x = n * sxx - sx * sx
    denom_y = n * syy - sy * sy
    cov_n = n * sxy - sx * sy
    defined = (n >= 2.0) & (denom_x > 0.0)
    defined_corr = defined & (denom_y > 0.0)
    return long.with_columns(
        [
            pl.when(defined).then(cov_n / denom_x).otherwise(None).alias("slope"),
            pl.when(defined_corr).then(cov_n / (denom_x * denom_y).sqrt()).otherwise(None).alias("corr"),
            pl.when(defined_corr).then((cov_n * cov_n) / (denom_x * denom_y)).otherwise(None).alias("r2"),
            pl.when(n > 0).then(sy / n).otherwise(None).alias("mean_y"),
        ]
    ).select(["symbol", "window", "slope", "corr", "r2", "mean_y"])


def windowed_corr_latest(
    frame: pl.DataFrame, pairs: list[tuple[str, str]], windows: tuple[int, ...] | list[int], by: str = "minute"
) -> pl.DataFrame:
    """Aggregate-at-T Pearson correlation for MANY (x, y) pairs in ONE windowed_sums pass — for when a
    group needs several correlations but none of the rest of the OLS (slope/r2). Each pair correlates only
    rows where BOTH x and y are present (the same pairing rule as ``windowed_ols_latest``, so the numbers
    match its ``corr`` exactly). Returns LONG (symbol, window, _corr0, _corr1, …) — pair i's correlation is
    ``_corr{i}``. The caller pivots each into its named per-window feature columns."""
    derived: dict[str, pl.Expr] = {}
    for i, (x, y) in enumerate(pairs):
        both = pl.col(x).is_not_null() & pl.col(y).is_not_null()
        derived[f"_cb{i}"] = both.cast(pl.Float64)
        derived[f"_cx{i}"] = pl.when(both).then(pl.col(x)).otherwise(0.0)
        derived[f"_cy{i}"] = pl.when(both).then(pl.col(y)).otherwise(0.0)
    prepared = frame.with_columns([expr.alias(name) for name, expr in derived.items()])
    products: dict[str, pl.Expr] = {}
    for i in range(len(pairs)):
        products[f"_cxy{i}"] = pl.col(f"_cx{i}") * pl.col(f"_cy{i}")
        products[f"_cxx{i}"] = pl.col(f"_cx{i}") * pl.col(f"_cx{i}")
        products[f"_cyy{i}"] = pl.col(f"_cy{i}") * pl.col(f"_cy{i}")
    prepared = prepared.with_columns([expr.alias(name) for name, expr in products.items()])
    value_cols = [
        col for i in range(len(pairs)) for col in (f"_cb{i}", f"_cx{i}", f"_cy{i}", f"_cxy{i}", f"_cxx{i}", f"_cyy{i}")
    ]
    long = rust_windowed_sums(prepared, value_cols, windows, by=by)
    corr_exprs = []
    for i in range(len(pairs)):
        n, sx, sy = pl.col(f"_cb{i}"), pl.col(f"_cx{i}"), pl.col(f"_cy{i}")
        sxy, sxx, syy = pl.col(f"_cxy{i}"), pl.col(f"_cxx{i}"), pl.col(f"_cyy{i}")
        cov_n = n * sxy - sx * sy
        denom_x = n * sxx - sx * sx
        denom_y = n * syy - sy * sy
        defined = (n >= 2.0) & (denom_x > 0.0) & (denom_y > 0.0)
        corr_exprs.append(pl.when(defined).then(cov_n / (denom_x * denom_y).sqrt()).otherwise(None).alias(f"_corr{i}"))
    return long.with_columns(corr_exprs).select(["symbol", "window", *[f"_corr{i}" for i in range(len(pairs))]])


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
