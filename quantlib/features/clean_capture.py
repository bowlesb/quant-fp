"""Glue between the clean ``CleanEngine`` and the live capture write path (``process_bars``).

The clean engine speaks numpy: ``emit(minute_bars)`` returns ``(present_symbols, {group: {feature: (n_sym,)
array}})`` aligned to the engine's fixed symbol index, already NaN'd outside the present mask (#57). The existing
write loop speaks polars: one ``(symbol, minute, *feature_names)`` frame per group, one row per present symbol.
This module holds BOTH directions of the glue: ``minute_frame_to_bars`` marshals THIS minute's bars (the polars
frame the capture path already materializes) into the engine's numpy minute dict, and ``emit_to_frames``
reshapes the engine's numpy output back into the per-group ``(symbol, minute, *features)`` frames the write loop
consumes. Pure plumbing, no compute, unit-testable in isolation.

The engine's column set is the UNION of every clean group's ``input_cols`` (the enriched bar columns the groups
read — close/high/low/open/volume plus signed_volume / quote spread+imbalance / tick-derived); a column a given
minute's frame lacks is filled NaN (an honest "not delivered", never fabricated). The bar values come from THIS
minute's frame, not the materialized buffer — the engine carries its own trailing ring.
"""

from __future__ import annotations

import datetime

import numpy as np
import polars as pl

_UTC = datetime.timezone.utc


def minute_frame_to_bars(
    frame: pl.DataFrame, cols: tuple[str, ...], minute_epoch: int
) -> dict[str, np.ndarray]:
    """Marshal THIS minute's bar frame (``symbol`` + the bar columns) into the engine's ``step``/``emit`` input
    dict: ``{"symbol": (n,) str array, <col>: (n,) float64 array for each col in ``cols``, "minute_epoch":
    (1,) int64}``. ``cols`` is the engine's column set (the union of every group's ``input_cols``); a column the
    frame lacks is filled NaN (honest absence). ``frame`` is THIS minute's rows only (one per present symbol) —
    NOT the trailing buffer, which the engine carries itself. ``minute_epoch`` is epoch-seconds for the watermark
    + the time-window stamps.

    STRUCTURAL INPUT GATE (guards the #80 class — a whole-buffer frame here feeds the engine duplicate/stale
    symbols and silently corrupts the cross-sectional present-set; a value-diff canary can be BLIND to it, so
    assert the contract directly): the frame must be ONE minute's bars — exactly one row per symbol (no duplicate
    symbols), and a single distinct ``minute`` when the column is present. Cheap, fires loudly on a marshal
    regression rather than corrupting features silently."""
    symbols = frame["symbol"].to_numpy()
    n = len(symbols)
    n_distinct = len(set(symbols.tolist()))
    if n != n_distinct:
        raise ValueError(
            f"minute_frame_to_bars got a non-this-minute frame: {n} rows but {n_distinct} distinct symbols "
            f"(duplicate symbols = whole-buffer marshal, the #80 bug). Filter the frame to the latest minute "
            f"before marshaling."
        )
    if "minute" in frame.columns and frame["minute"].n_unique() > 1:
        raise ValueError(
            f"minute_frame_to_bars got {frame['minute'].n_unique()} distinct minutes; expected exactly one (the "
            f"trailing buffer must NOT be marshaled — the engine carries its own ring)."
        )
    minute_bars: dict[str, np.ndarray] = {
        "symbol": symbols,
        "minute_epoch": np.array([minute_epoch], dtype=np.int64),
    }
    for col in cols:
        if col in frame.columns:
            minute_bars[col] = frame[col].to_numpy().astype(np.float64)
        else:
            minute_bars[col] = np.full(n, np.nan, dtype=np.float64)
    return minute_bars


def emit_to_frames(
    present_symbols: list[str],
    features: dict[str, dict[str, np.ndarray]],
    symbols: list[str],
    minute_epoch: int,
) -> dict[str, pl.DataFrame]:
    """Reshape one minute of clean-engine ``emit`` output into per-group ``(symbol, minute, *features)`` frames
    — the shape the write loop / ``store.write_group`` consumes. ONE row per PRESENT symbol per group (the
    engine's arrays are ``(n_symbols,)`` aligned to ``symbols``; absent symbols are already NaN and are dropped
    here by selecting only the present rows). ``minute_epoch`` is epoch-seconds; the emitted ``minute`` column is
    a UTC datetime, matching the legacy frames' ``minute`` dtype.

    Returns ``{group_name: frame}``; a group with no present symbols yields an empty (schema-correct) frame.
    """
    minute = datetime.datetime.fromtimestamp(minute_epoch, tz=_UTC)
    index = {symbol: position for position, symbol in enumerate(symbols)}
    present_positions = np.array([index[symbol] for symbol in present_symbols], dtype=np.int64)
    out: dict[str, pl.DataFrame] = {}
    for group_name, feature_arrays in features.items():
        columns: dict[str, object] = {
            "symbol": present_symbols,
            "minute": [minute] * len(present_symbols),
        }
        for feature_name, values in feature_arrays.items():
            columns[feature_name] = values[present_positions]
        frame = pl.DataFrame(columns)
        out[group_name] = frame.with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    return out
