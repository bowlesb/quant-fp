"""Parquet feature store — per-group, per-source partitioned writes + the ``get_features`` read API.

Layout (FEATURE_PLATFORM.md §3.3.1): ``<root>/group=<g>/v=<ver>/source=<stream|backfill>/date=<d>/data.parquet``.

We store features written BOTH ways and track which: ``source=stream`` are the provisional values the
running system computed live; ``source=backfill`` are the settled values recomputed from the
historical tape (truth, available ~T+1). Both are retained — the T+1 parity test compares them, and
a model trains on backfill but infers on live, so the train/serve gap IS the stream-vs-backfill
difference. ``get_features(source="auto")`` returns backfill where available, stream for the
unsettled recent window. Writes are atomic (write-temp-then-rename); reads are column-pruned scans.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import polars as pl

from quantlib.features.base import KEY_COLUMNS
from quantlib.features.registry import REGISTRY

SOURCES = ("backfill", "stream")  # priority order for source="auto" (backfill = truth, preferred)


def _partition_dir(root: str | Path, group: str, version: str, source: str, day: str) -> Path:
    return Path(root) / f"group={group}" / f"v={version}" / f"source={source}" / f"date={day}"


def write_group(
    root: str | Path, group: str, version: str, source: str, day: str, frame: pl.DataFrame
) -> Path:
    """Write one group's features for one day+source. Atomic + idempotent (rerun overwrites cleanly)."""
    target = _partition_dir(root, group, version, source, day)
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


def _scan_source(
    root: str | Path, group: str, version: str, source: str, feats: list[str],
    symbols: list[str] | str, start: dt.datetime, end: dt.datetime,
) -> pl.DataFrame:
    files = list(Path(root).glob(f"group={group}/v={version}/source={source}/date=*/data.parquet"))
    if not files:
        return pl.DataFrame()
    frame = pl.scan_parquet(files).select([*KEY_COLUMNS, *feats])
    if symbols != "universe":
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    return frame.filter((pl.col("minute") >= start) & (pl.col("minute") <= end)).collect()


def get_features(
    names: list[str],
    symbols: list[str] | str,
    start: dt.datetime,
    end: dt.datetime,
    root: str | Path,
    source: str = "auto",
) -> pl.DataFrame:
    """Tidy frame keyed (symbol, minute), one column per requested feature, sorted, point-in-time.
    ``source``: "auto" returns backfill where available else stream (best-truth); or "stream"/"backfill"
    for a specific source (e.g. for parity). RAISES on an unknown/uncertified feature."""
    by_group: dict[tuple[str, str], list[str]] = {}
    for name in names:
        by_group.setdefault(_resolve(name), []).append(name)

    result: pl.DataFrame | None = None
    for (group, version), feats in by_group.items():
        if source == "auto":
            part = _scan_source(root, group, version, "backfill", feats, symbols, start, end)
            stream = _scan_source(root, group, version, "stream", feats, symbols, start, end)
            if part.height == 0:
                part = stream
            elif stream.height:
                extra = stream.join(part.select(list(KEY_COLUMNS)), on=list(KEY_COLUMNS), how="anti")
                part = pl.concat([part, extra])
        else:
            part = _scan_source(root, group, version, source, feats, symbols, start, end)
        result = part if result is None else result.join(part, on=list(KEY_COLUMNS), how="full", coalesce=True)
    return result.sort(list(KEY_COLUMNS)) if result is not None and result.height else (result or pl.DataFrame())


def drop_before(root: str | Path, cutoff_day: str) -> list[Path]:
    """Retention: remove date partitions strictly older than cutoff_day (R11 free-disk floor)."""
    removed = []
    for date_dir in Path(root).glob("group=*/v=*/source=*/date=*"):
        if date_dir.name.removeprefix("date=") < cutoff_day:
            shutil.rmtree(date_dir)
            removed.append(date_dir)
    return removed
