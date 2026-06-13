"""Feature-store lifecycle management: status, completeness, delete/retire — with restore recipes.

CORE PRINCIPLE: ``source=backfill`` is a REPRODUCIBLE CACHE — its source of truth is the group code
(git) + Alpaca (settled history), so deleting it is always safe (restore = re-materialize). BUT
``source=stream`` is the LIVE provisional capture and is NOT reproducible (Alpaca only serves
settled data) — once a past day's stream is gone, the train/serve-gap record and that day's parity
reference are gone forever. So deletions REFUSE stream partitions unless explicitly forced. Every
deletion logs a restore recipe to ``RETIREMENT_LOG``.

Supports the iteration lifecycle: an abandoned partial backfill (verify what's there, resume the
rest, or delete it), and disk reclamation (retire old dates / drop a low-value feature, restorable).
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import polars as pl

RETIREMENT_LOG = "RETIREMENT_LOG.jsonl"


def _iter_partitions(root: str | Path):
    root = Path(root)
    for path in root.glob("group=*/v=*/source=*/date=*"):
        rel = path.relative_to(root).parts
        yield {
            "path": path,
            "group": rel[0].removeprefix("group="),
            "version": rel[1].removeprefix("v="),
            "source": rel[2].removeprefix("source="),
            "date": rel[3].removeprefix("date="),
        }


def _bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def store_status(root: str | Path) -> pl.DataFrame:
    """Per (group, version, source): date count, date range, and disk MB — the accounting view."""
    rows = [
        {"group": p["group"], "version": p["version"], "source": p["source"], "date": p["date"], "bytes": _bytes(p["path"])}
        for p in _iter_partitions(root)
    ]
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).group_by(["group", "version", "source"]).agg(
        pl.col("date").n_unique().alias("dates"),
        pl.col("date").min().alias("from"),
        pl.col("date").max().alias("to"),
        (pl.col("bytes").sum() / 1e6).round(2).alias("mb"),
    ).sort(["group", "source"])


def completeness(root: str | Path, group: str, version: str, source: str, expected_dates: list[str]) -> dict:
    """Verify a (possibly abandoned/partial) backfill: which expected dates are present vs missing."""
    present = {p["date"] for p in _iter_partitions(root) if p["group"] == group and p["version"] == version and p["source"] == source}
    expected = set(expected_dates)
    done = expected & present
    return {
        "expected": len(expected),
        "present": len(done),
        "missing": sorted(expected - present),
        "pct": round(100.0 * len(done) / len(expected), 1) if expected else 0.0,
    }


def _log(root: str | Path, entry: dict) -> None:
    with (Path(root) / RETIREMENT_LOG).open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _delete(root: str | Path, matching, action: str, restore: str, include_stream: bool = False) -> dict:
    parts = [p for p in _iter_partitions(root) if matching(p)]
    stream_parts = [p for p in parts if p["source"] == "stream"]
    if stream_parts and not include_stream:
        raise ValueError(
            f"{action}: refusing to delete {len(stream_parts)} source=stream partition(s) — live "
            f"stream is NOT reproducible (Alpaca only serves settled backfill). Pass "
            f"include_stream=True to override and accept PERMANENT loss."
        )
    freed = sum(_bytes(p["path"]) for p in parts)
    dates = sorted({p["date"] for p in parts})
    for p in parts:
        shutil.rmtree(p["path"])
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "action": action,
        "partitions": len(parts),
        "dates": dates,
        "mb_freed": round(freed / 1e6, 2),
        "restore": restore,
    }
    if parts:
        _log(root, entry)
    return entry


def delete_feature_group(root: str | Path, group: str, version: str | None = None, include_stream: bool = False) -> dict:
    """Wipe a feature group's BACKFILL partitions (code stays in git → re-materializable). Stream
    partitions are protected unless ``include_stream=True`` (they are irreplaceable)."""
    restore = f"re-materialize group '{group}' backfill via `materialize alpaca <root> <day> <n>` (code in git); stream is NOT re-collectable for past days"
    return _delete(root, lambda p: p["group"] == group and (version is None or p["version"] == version), f"delete_feature_group:{group}", restore, include_stream)


def retire_before(root: str | Path, before_date: str, include_stream: bool = False) -> dict:
    """Reclaim disk: wipe BACKFILL partitions older than ``before_date`` (re-backfillable from
    Alpaca). Stream partitions are protected unless ``include_stream=True``."""
    restore = f"re-backfill dates < {before_date} from Alpaca via `materialize alpaca <root> <day> <n>` (settled bars reproducible; stream is not)"
    return _delete(root, lambda p: p["date"] < before_date, f"retire_before:{before_date}", restore, include_stream)
