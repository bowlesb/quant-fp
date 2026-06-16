"""Parquet feature store — per-group, per-source partitioned writes + the ``get_features`` read API.

Layout (FEATURE_PLATFORM.md §3.3.1): ``<root>/group=<g>/v=<ver>/source=<stream|sim|backfill>/date=<d>/data.parquet``.

We store features written BOTH ways and track which: ``source=stream`` are the provisional values the
running REAL system computed live; ``source=sim`` are the provisional values a MOCK/sim run computed
live (mode-separated, so the simulation exercises the EXACT real path but never pollutes the real
``source=stream``); ``source=backfill`` are the settled values recomputed from the
historical tape (truth, available ~T+1). Both are retained — the T+1 parity test compares them, and
a model trains on backfill but infers on live, so the train/serve gap IS the stream-vs-backfill
difference. ``get_features(source="auto")`` returns backfill where available, stream for the
unsettled recent window. Writes are atomic (write-temp-then-rename); reads are column-pruned scans.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from functools import lru_cache
from pathlib import Path

import polars as pl

from quantlib.features.base import KEY_COLUMNS, storage_dtype
from quantlib.features.registry import REGISTRY

LIVE_ZSTD_LEVEL = 1  # per-minute append writes: latency-sensitive, tiny files -> fast compression
BATCH_ZSTD_LEVEL = 19  # backfill / compaction: written once, read for training -> max ratio (tight disk)

SOURCES = ("backfill", "stream", "sim")  # priority order for source="auto" (backfill = truth, preferred)
MODE_FILE = "_store_mode"  # "real" | "mock" — physically separates real and simulated data

# The live write source is DERIVED from the run mode so simulated data never lands under the real
# ``source=stream``: a real Alpaca run (mode='real') writes ``source=stream``; a mock/sim run
# (mode='mock') writes ``source=sim``. Backfill is written explicitly as ``source=backfill`` (truth).
_MODE_SOURCE = {"real": "stream", "mock": "sim"}


def source_for_mode(mode: str) -> str:
    """The live write source for a capture ``mode``: real->'stream', mock->'sim'. A mock/sim run is
    thus physically separated under ``source=sim``, so it can exercise the EXACT real path while never
    polluting the real provisional ``source=stream`` partitions."""
    if mode not in _MODE_SOURCE:
        raise ValueError(f"capture mode must be 'real' or 'mock', got {mode!r}")
    return _MODE_SOURCE[mode]


def _partition_dir(root: str | Path, group: str, version: str, source: str, day: str) -> Path:
    return Path(root) / f"group={group}" / f"v={version}" / f"source={source}" / f"date={day}"


@lru_cache(maxsize=None)
def _group_storage_dtypes(group: str) -> dict[str, pl.DataType]:
    """Per-group {feature -> on-disk dtype} from the declared specs (Float32 / nullable UInt8 / small int)
    — cached so the per-minute write path doesn't re-walk the registry every tick."""
    return {spec.name: storage_dtype(spec) for group_obj, spec in REGISTRY.feature_specs() if group_obj.name == group}


def _cast_to_storage(frame: pl.DataFrame, group: str) -> pl.DataFrame:
    """Downcast a group's feature columns to their storage dtypes (~54% smaller than Float64). Key columns
    are untouched. Computation stays Float64; only the persisted copy is narrowed (parity is stored-vs-
    stored, so both sides round identically)."""
    dtypes = _group_storage_dtypes(group)
    casts = [pl.col(name).cast(dtype) for name, dtype in dtypes.items() if name in frame.columns]
    return frame.with_columns(casts) if casts else frame


def store_mode(root: str | Path) -> str | None:
    marker = Path(root) / MODE_FILE
    return marker.read_text().strip() if marker.exists() else None


def set_mode(root: str | Path, mode: str) -> None:
    """Tag a store root as 'real' or 'mock' and REFUSE to mix — mock streaming data can never be
    written into the real store (or vice versa), so simulated data is never confused for real."""
    if mode not in ("real", "mock"):
        raise ValueError(f"store mode must be 'real' or 'mock', got {mode!r}")
    Path(root).mkdir(parents=True, exist_ok=True)
    marker = Path(root) / MODE_FILE
    existing = marker.read_text().strip() if marker.exists() else None
    if existing is not None and existing != mode:
        raise ValueError(f"store '{root}' is mode '{existing}'; refusing to write '{mode}' data (mock/real separation)")
    if existing is None:
        marker.write_text(mode)


def write_group(
    root: str | Path,
    group: str,
    version: str,
    source: str,
    day: str,
    frame: pl.DataFrame,
    mode: str = "real",
    shard: int | None = None,
    minute: dt.datetime | None = None,
) -> Path:
    """Write one group's features for one day+source. Atomic + idempotent (rerun overwrites cleanly).

    ``shard`` enables CONCURRENT partition-disjoint writes: each capture worker passes its shard id and
    writes its OWN file inside the partition, via a per-file temp + atomic ``os.replace`` (POSIX-atomic,
    same dir) — so N workers writing the same (group, date) never clobber or contend (Monday collect+save
    concurrency, requirement #6). ``shard=None`` writes the single ``data.parquet`` (backfill / repair).

    ``minute`` switches the STREAMING write to APPEND mode: the file is named per-minute
    (``data-<shard>-<epoch>.parquet``), so each minute writes ONLY that minute's rows — O(1) per tick, no
    rewriting the day's accumulated history (which would be O(minutes²) I/O and fall over intraday). A
    re-delivered minute overwrites its own file (same name → atomic replace), staying idempotent. Reads
    glob ``data*.parquet`` so every per-minute file reads back as the union. ``mode`` ('real'|'mock') is
    enforced per root so simulated data never lands in the real store.
    """
    set_mode(root, mode)
    target = _partition_dir(root, group, version, source, day)
    target.mkdir(parents=True, exist_ok=True)
    if minute is not None:
        stamp = int(minute.timestamp())
        name = f"data-{stamp}.parquet" if shard is None else f"data-{shard}-{stamp}.parquet"
    else:
        name = "data.parquet" if shard is None else f"data-{shard}.parquet"
    # temp name starts with '.tmp-' so it never matches the data*.parquet read glob, even mid-write.
    tmp = target / f".tmp-{name}.{os.getpid()}"
    level = LIVE_ZSTD_LEVEL if minute is not None else BATCH_ZSTD_LEVEL
    _cast_to_storage(frame, group).write_parquet(tmp, compression="zstd", compression_level=level)
    os.replace(tmp, target / name)  # atomic rename within the same dir; no whole-partition clobber
    return target / name


def _resolve(name: str) -> tuple[str, str]:
    for group, spec in REGISTRY.feature_specs():
        if spec.name == name:
            return group.name, group.version
    raise KeyError(f"unknown/uncertified feature '{name}'")


def _date_dirs(root: str | Path, group: str, version: str, source: str) -> set[str]:
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    return {p.name.removeprefix("date=") for p in base.glob("date=*")} if base.exists() else set()


def settled_dates(root: str | Path, group: str, version: str) -> set[str]:
    """Dates with a settled backfill partition for a group (the train-eligible days)."""
    return _date_dirs(root, group, version, "backfill")


def _scan_source(
    root: str | Path,
    group: str,
    version: str,
    source: str,
    feats: list[str],
    symbols: list[str] | str,
    start: dt.datetime,
    end: dt.datetime,
) -> pl.DataFrame:
    files = list(Path(root).glob(f"group={group}/v={version}/source={source}/date=*/data*.parquet"))
    if not files:
        return pl.DataFrame()
    # Features are stored narrowed (Float32 / nullable UInt8 / small int); widen back to Float64 on read so
    # every consumer (parity diff, training export, API) sees the uniform compute dtype — the narrowing is a
    # pure disk concern. (Without this, UInt8 flags would integer-underflow in the parity subtraction.)
    frame = pl.scan_parquet(files).select([*KEY_COLUMNS, *[pl.col(name).cast(pl.Float64) for name in feats]])
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
    require_settled: bool = False,
) -> pl.DataFrame:
    """Tidy frame keyed (symbol, minute), one column per requested feature, sorted, point-in-time.
    ``source``: "auto" returns backfill where available else stream (best-truth); or "stream"/"backfill"
    for a specific source (e.g. for parity). ``require_settled=True`` (use for TRAINING reads) RAISES
    if any requested date is stream-only — so a model never trains on provisional, unsettled data.
    RAISES on an unknown/uncertified feature."""
    by_group: dict[tuple[str, str], list[str]] = {}
    for name in names:
        by_group.setdefault(_resolve(name), []).append(name)

    # ``auto`` merges settled truth with the provisional live source. The provisional source is the one
    # this store mode writes: ``stream`` for a real store, ``sim`` for a mock store (mode-separated roots
    # never hold both), so a sim store reads its own ``source=sim`` under ``auto``.
    provisional = "sim" if store_mode(root) == "mock" else "stream"

    if require_settled:
        for (group, version), _ in by_group.items():
            unsettled = _date_dirs(root, group, version, provisional) - _date_dirs(root, group, version, "backfill")
            in_range = {d for d in unsettled if start.date() <= dt.date.fromisoformat(d) <= end.date()}
            if in_range:
                raise ValueError(
                    f"require_settled: group '{group}' has unsettled ({provisional}-only) dates {sorted(in_range)} "
                    f"in range — backfill them before using for training"
                )

    result: pl.DataFrame | None = None
    for (group, version), feats in by_group.items():
        if source == "auto":
            part = _scan_source(root, group, version, "backfill", feats, symbols, start, end)
            stream = _scan_source(root, group, version, provisional, feats, symbols, start, end)
            if part.height == 0:
                part = stream
            elif stream.height:
                extra = stream.join(part.select(list(KEY_COLUMNS)), on=list(KEY_COLUMNS), how="anti")
                part = pl.concat([part, extra])
        else:
            part = _scan_source(root, group, version, source, feats, symbols, start, end)
        result = part if result is None else result.join(part, on=list(KEY_COLUMNS), how="full", coalesce=True)
    if result is not None and result.height:
        return result.sort(list(KEY_COLUMNS))
    return result if result is not None else pl.DataFrame()


def stream_symbols_on(root: str | Path, day: str, source: str = "stream") -> list[str]:
    """The DISTINCT symbols that were collected live (``source=stream``) on ``day`` — the universe the
    nightly parity sweep must validate. Unions the ``symbol`` column across every group's partition for
    the day (read column-pruned, so feature values are never materialized). A mock store would pass
    ``source='sim'``. Empty when nothing was captured that day.

    Each file is read with its own scan (per-file ``symbol`` projection) rather than one multi-file scan:
    different groups have different schemas, so a single ``scan_parquet`` over the mixed file list would
    reject the column-superset — reading the one shared key column per file sidesteps that.
    """
    files = list(Path(root).glob(f"group=*/v=*/source={source}/date={day}/data*.parquet"))
    if not files:
        return []
    symbols: set[str] = set()
    for file in files:
        symbols.update(pl.read_parquet(file, columns=["symbol"])["symbol"].to_list())
    return sorted(symbols)


def drop_before(root: str | Path, cutoff_day: str) -> list[Path]:
    """Retention: remove date partitions strictly older than cutoff_day (R11 free-disk floor)."""
    removed = []
    for date_dir in Path(root).glob("group=*/v=*/source=*/date=*"):
        if date_dir.name.removeprefix("date=") < cutoff_day:
            shutil.rmtree(date_dir)
            removed.append(date_dir)
    return removed
