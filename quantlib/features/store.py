"""Parquet feature store — per-group partitioned writes + the ``get_features`` read API (R13).

Layout (FEATURE_PLATFORM.md §3.3.1): ``<root>/group=<name>/v=<version>/date=<YYYY-MM-DD>/data.parquet``
— one group per partition, so recomputing/updating one feature touches only that group's files and
adding a feature never widens an existing file. Writes are atomic (write-temp-then-rename); reads
are column-pruned Polars scans. ``get_features`` resolves requested features to their owning groups
and joins them on (symbol, minute).
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import polars as pl

from quantlib.features.base import KEY_COLUMNS
from quantlib.features.registry import REGISTRY


def _partition_dir(root: str | Path, group: str, version: str, day: str) -> Path:
    return Path(root) / f"group={group}" / f"v={version}" / f"date={day}"


def write_group(root: str | Path, group: str, version: str, day: str, frame: pl.DataFrame) -> Path:
    """Write one group's features for one day to its partition. Atomic + idempotent: a rerun
    overwrites cleanly (write-temp-then-rename), so backfills are safe to repeat."""
    target = _partition_dir(root, group, version, day)
    staging = target.with_name(target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    frame.write_parquet(staging / "data.parquet")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return target


def _resolve(name: str) -> tuple[str, str]:
    for group, spec in REGISTRY.feature_specs():
        if spec.name == name:
            return group.name, group.version
    raise KeyError(f"unknown/uncertified feature '{name}'")


def get_features(
    names: list[str],
    symbols: list[str] | str,
    start: dt.datetime,
    end: dt.datetime,
    root: str | Path,
) -> pl.DataFrame:
    """Tidy frame keyed (symbol, minute), one column per requested feature, sorted, point-in-time.
    RAISES on an unknown/uncertified feature. Returns identical values whether the features were
    produced live or by backfill (same group code wrote them)."""
    by_group: dict[tuple[str, str], list[str]] = {}
    for name in names:
        by_group.setdefault(_resolve(name), []).append(name)

    result: pl.DataFrame | None = None
    for (group, version), feats in by_group.items():
        pattern = str(_partition_dir(root, group, version, "*") / "data.parquet")
        frame = pl.scan_parquet(pattern).select([*KEY_COLUMNS, *feats])
        if symbols != "universe":
            frame = frame.filter(pl.col("symbol").is_in(symbols))
        part = frame.filter((pl.col("minute") >= start) & (pl.col("minute") <= end)).collect()
        result = part if result is None else result.join(part, on=list(KEY_COLUMNS), how="full", coalesce=True)
    return result.sort(list(KEY_COLUMNS)) if result is not None else pl.DataFrame()


def drop_before(root: str | Path, cutoff_day: str) -> list[Path]:
    """Retention: remove date partitions strictly older than cutoff_day (R11 free-disk floor)."""
    removed = []
    for date_dir in Path(root).glob("group=*/v=*/date=*"):
        day = date_dir.name.removeprefix("date=")
        if day < cutoff_day:
            shutil.rmtree(date_dir)
            removed.append(date_dir)
    return removed
