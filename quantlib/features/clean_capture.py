"""Glue between the clean ``CleanEngine`` and the live capture write path (``process_bars``).

The clean engine speaks numpy: ``emit(minute_bars)`` returns ``(present_symbols, {group: {feature: (n_sym,)
array}})`` aligned to the engine's fixed symbol index, already NaN'd outside the present mask (#57). The existing
write loop speaks polars: one ``(symbol, minute, *feature_names)`` frame per group, one row per present symbol.
This module is the RESHAPE between them — pure plumbing, no compute, unit-testable in isolation, coupled to no
go-live decision. The marshal in the OTHER direction (this-minute's bars → the engine's numpy minute dict) lives
beside it once the per-session-rebuild question is settled; this file is the decision-independent half.
"""

from __future__ import annotations

import datetime

import numpy as np
import polars as pl

_UTC = datetime.timezone.utc


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
