"""Rust slice-derive: resolve the per-symbol ``shift(k).over("symbol")`` lags in a single ordered Rust pass
so the incremental engine's value-column derive runs GLOBALLY (no Polars per-symbol partition).

The incremental V2 slice-derive (``IncrementalEngine._derived_row``) computes ~short-lag value columns over a
~6-minute slice. Profiling at 1250×60 shows the whole ~53ms cost is Polars re-partitioning the slice by symbol
for each ``shift(k).over("symbol")`` expression — the surrounding arithmetic (logs, products, conditionals) is
trivial. Every grouped op in that derive IS a positional ``shift`` of a single input column (only ``close``,
lags 1/2/3, in the current feature set); nothing else needs the partition.

This module:
  1. ``lag_specs`` — scans a list of derive exprs and finds every distinct ``Column(c).shift(k).over("symbol")``
     (column name + lag), the lags the Rust kernel must resolve. Raises if an ``over("symbol")`` wraps anything
     other than a plain-column ``shift`` (the engine handles ``cum_sum``/time regressors as stateful state, so
     they never reach here — anything else would silently break parity, so we fail loudly).
  2. ``rewrite_global`` — rewrites those exprs, replacing each ``Column(c).shift(k).over("symbol")`` with a
     plain ``Column("__lag{k}_{c}")``, so the rewritten expr has NO ``over`` and evaluates correctly on a
     one-row-per-symbol frame that carries the lag columns.
  3. ``rust_slice_derive`` — calls the ``quant_tick.slice_derive_lags`` kernel on the symbol-coded, minute-
     sorted slice and returns the one-row-per-symbol latest frame augmented with ``__lag{k}_{c}`` columns. A
     missing prior bar (warmup) is the kernel's NaN sentinel, converted back to Polars ``null`` so the rewritten
     exprs see EXACTLY the ``null`` that ``shift(k).over("symbol")`` would have produced (so ``null``-propagation
     and ``when`` guards behave identically — the whole point of doing this parity-true).

PARITY: the rewritten global derive on the kernel's lag frame is cell-for-cell identical to the Polars
``shift().over()`` derive at the latest row of each symbol, because (a) the slice is minute-contiguous per
symbol so the L-th prior ROW is the L-th prior MINUTE (matching positional ``shift``), (b) the latest row sits
at each symbol-block's end so a within-block backward lag never crosses a symbol boundary, and (c) the missing-
prior-bar cells are ``null`` in both. Guarded cell-for-cell by tests/test_fp_slice_derive_rust.py.
"""

from __future__ import annotations

import io
import json
from typing import Any

import polars as pl
import quant_tick

_OVER_SYMBOL = [{"Column": "symbol"}]


def _walk_collect(node: Any, lags: set[tuple[str, int]]) -> None:
    """Collect every ``Column(c).shift(k).over("symbol")`` (c, k) in the serialized expr tree; assert any
    ``over("symbol")`` is exactly that shape (the engine routes everything else to stateful state)."""
    if isinstance(node, dict):
        if "Over" in node:
            over = node["Over"]
            spec = _shift_over_spec(over)
            if spec is None:
                func = over.get("function", {}).get("Function", {}).get("function")
                raise ValueError(
                    "slice_derive: an over('symbol') in the derive is not a plain Column.shift(k) "
                    f"(found function={func!r}); only positional column shifts are slice-derivable in Rust. "
                    "Route long-history regressors through stateful_regressors() instead."
                )
            lags.add(spec)
        for value in node.values():
            _walk_collect(value, lags)
    elif isinstance(node, list):
        for item in node:
            _walk_collect(item, lags)


def _shift_over_spec(over: dict[str, Any]) -> tuple[str, int] | None:
    """If ``over`` is ``Column(c).shift(k).over("symbol")`` return (c, k); else None."""
    if over.get("partition_by") != _OVER_SYMBOL or over.get("order_by") is not None:
        return None
    func = over.get("function", {}).get("Function", {})
    if func.get("function") != "Shift":
        return None
    inputs = func.get("input", [])
    if len(inputs) != 2 or "Column" not in inputs[0]:
        return None
    column = inputs[0]["Column"]
    literal = inputs[1].get("Literal", {}).get("Dyn", {})
    if "Int" not in literal:
        return None
    return column, int(literal["Int"])


def lag_specs(exprs: list[pl.Expr]) -> tuple[set[tuple[str, int]], int]:
    """The distinct (column, lag) pairs every ``shift(k).over('symbol')`` in ``exprs`` needs, and the max lag."""
    lags: set[tuple[str, int]] = set()
    for expr in exprs:
        _walk_collect(json.loads(expr.meta.serialize(format="json")), lags)
    max_lag = max((lag for _, lag in lags), default=0)
    return lags, max_lag


def _rewrite_node(node: Any) -> Any:
    """Replace each ``Column(c).shift(k).over('symbol')`` subtree with a plain ``Column('__lag{k}_{c}')``."""
    if isinstance(node, dict):
        if "Over" in node:
            spec = _shift_over_spec(node["Over"])
            if spec is not None:
                column, lag = spec
                return {"Column": f"__lag{lag}_{column}"}
        return {key: _rewrite_node(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite_node(item) for item in node]
    return node


def rewrite_global(expr: pl.Expr) -> pl.Expr:
    """``expr`` with every ``Column(c).shift(k).over('symbol')`` swapped for ``pl.col('__lag{k}_{c}')`` — so it
    has no per-symbol partition and evaluates correctly on a one-row-per-symbol frame carrying the lag cols."""
    tree = _rewrite_node(json.loads(expr.meta.serialize(format="json")))
    return pl.Expr.deserialize(io.StringIO(json.dumps(tree)), format="json")


def rust_slice_derive(
    slice_frame: pl.DataFrame,
    input_cols: list[str],
    lag_columns: dict[str, list[int]],
    minute: object,
) -> pl.DataFrame:
    """One-row-per-symbol latest frame + ``__lag{k}_{col}`` columns from the Rust lag kernel.

    ``slice_frame`` is the trailing slice (the input columns + symbol + minute); ``lag_columns`` maps a column
    name -> the lags of it that the derive needs. The kernel resolves those lags per symbol in one ordered pass;
    a missing prior bar comes back as NaN and is restored to Polars ``null`` so the rewritten derive sees the
    same ``null`` ``shift().over()`` would. The returned frame is symbol-sorted (matching ``_derived_row``)."""
    columns = sorted(lag_columns)
    max_lag = max((max(lags) for lags in lag_columns.values()), default=0)
    select_cols = list(dict.fromkeys(["symbol", "minute", *input_cols]))  # input_cols already holds symbol/minute
    prepared = (
        slice_frame.select(select_cols)
        .with_columns(pl.col("minute").dt.epoch("s").alias("_mi"))
        .sort(["symbol", "_mi"])
    )
    uniq = sorted(prepared["symbol"].unique().to_list())
    codes = pl.DataFrame({"symbol": uniq, "_c": list(range(len(uniq)))}, schema={"symbol": pl.String, "_c": pl.Int64})
    prepared = prepared.join(codes, on="symbol", how="left").sort(["_c", "_mi"])

    sym_codes = prepared["_c"].to_numpy()
    value_arrays = [prepared[col].to_numpy() for col in columns]
    out_sym, out_lags = quant_tick.slice_derive_lags(sym_codes, value_arrays, int(max_lag))

    reverse = dict(enumerate(uniq))
    latest = prepared.filter(pl.col("minute") == minute).sort("_c")
    data: dict[str, list[float]] = {}
    n_sym = len(out_sym)
    for col_idx, col in enumerate(columns):
        flat = out_lags[col_idx]
        for lag in lag_columns[col]:
            # column ``col``'s lag-``lag`` value for symbol si is at si*max_lag + (lag-1)
            data[f"__lag{lag}_{col}"] = [flat[si * max_lag + (lag - 1)] for si in range(n_sym)]
    lag_frame = pl.DataFrame({"symbol": [reverse[code] for code in out_sym], **data}).with_columns(
        # NaN sentinel (missing prior bar) -> Polars null, so rewritten exprs see shift().over()'s null
        [pl.when(pl.col(name).is_nan()).then(None).otherwise(pl.col(name)).alias(name) for name in data]
    )
    return latest.drop("_c", "_mi").join(lag_frame, on="symbol", how="left").sort("symbol")
