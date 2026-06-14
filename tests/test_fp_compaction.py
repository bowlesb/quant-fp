"""Compaction: the many per-minute streaming files fold into one compacted file with identical data.

Guards the T+1 tidy-up of the live append path — that merging per-(shard, minute) files de-dups
re-delivered minutes, preserves every cell, and leaves a single ``data-compacted.parquet`` the reader
still globs. Also pins that storage narrowing (Float32 / nullable UInt8) round-trips through read as the
Float64 compute dtype.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from quantlib.features import store
from quantlib.features.compact import COMPACTED_NAME, compact_day

BASE = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
DAY = "2026-06-15"


def _write_minute(root: str, shard: int, minute_index: int) -> None:
    minute = BASE + timedelta(minutes=minute_index)
    frame = pl.DataFrame(
        {
            "symbol": [f"S{shard}"],
            "minute": [minute],
            "ret_1m": [0.001 * minute_index],
            "is_bullish": [1.0 if minute_index % 2 == 0 else 0.0],  # flag -> stored UInt8
        }
    )
    store.write_group(root, "demo", "1.0.0", "stream", DAY, frame, mode="mock", shard=shard, minute=minute)


def test_compaction_folds_per_minute_files_preserving_data(tmp_path: Path) -> None:
    root = str(tmp_path / "store")
    for shard in (0, 1):
        for minute_index in range(4):
            _write_minute(root, shard, minute_index)
    _write_minute(root, 0, 2)  # RE-DELIVERY of shard 0 minute 2 -> must not duplicate

    partition = Path(root) / "group=demo" / "v=1.0.0" / "source=stream" / f"date={DAY}"
    before = list(partition.glob("data*.parquet"))
    assert len(before) == 8  # 2 shards x 4 minutes; the re-delivery OVERWROTE its minute-keyed file

    folded = compact_day(root, DAY)
    after = list(partition.glob("data*.parquet"))
    assert after == [partition / COMPACTED_NAME]  # exactly one file remains
    assert sum(folded.values()) == 8

    df = pl.read_parquet(partition / COMPACTED_NAME).sort("symbol", "minute")
    assert df.height == 8  # 2 symbols x 4 minutes, re-delivery de-duped (no duplicate cells)
    assert int(df.select(pl.struct("symbol", "minute").is_duplicated().sum()).item()) == 0
    assert df.filter((pl.col("symbol") == "S0") & (pl.col("minute") == BASE + timedelta(minutes=2)))["ret_1m"][0] == 0.002
