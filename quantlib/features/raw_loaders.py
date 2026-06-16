"""Read the already-downloaded ``/store/raw`` minute bars into the ``minute_agg`` frame.

The ACQUIRE stage (``quantlib.data.raw_backfill``) downloads raw minute bars ONCE into
``/store/raw/bars/symbol=<S>/date=<YYYY-MM-DD>/data.parquet``. The MATERIALIZE stage reads that raw
substrate instead of re-fetching from Alpaca — download-once, compute-many, with a clean segregation
between acquiring the tape and computing features from it.

The returned frame is cell-identical in schema to ``backfill_bars`` (the prior re-fetch path), so the
feature engine cannot tell which source produced it — parity by construction.
"""
from __future__ import annotations

import datetime as dt
import os

import polars as pl

from quantlib.data.raw_backfill import partition_dir
from quantlib.features.backfill_bars import BARS_SCHEMA


def load_raw_minute_agg(raw_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """Minute bars for ``symbols`` on ``day`` read from the ``/store/raw/bars`` partitions.

    Returns the SAME ``(symbol, minute, open, close, high, low, volume)`` shape as
    ``backfill_bars`` — ``minute`` is the raw ``ts`` column and ``volume`` is cast Int64 → Float64.
    Symbols with no partition on disk are absent from the result (mirroring how Alpaca returns no rows
    for a no-data symbol), so the caller never needs to special-case missing raw days.
    """
    target_day = dt.date.fromisoformat(day)
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        path = os.path.join(partition_dir(raw_root, "bars", symbol, target_day), "data.parquet")
        if not os.path.exists(path):
            continue
        raw = pl.read_parquet(path)
        frame = raw.select(
            pl.col("symbol"),
            pl.col("ts").alias("minute"),
            pl.col("open"),
            pl.col("close"),
            pl.col("high"),
            pl.col("low"),
            pl.col("volume").cast(pl.Float64),
        ).cast(BARS_SCHEMA)
        if frame.height:
            frames.append(frame)
    if not frames:
        return pl.DataFrame(schema=BARS_SCHEMA)
    return pl.concat(frames, how="vertical")
