"""Validation ledger store — durable real-time-vs-backfill verdicts + per-feature trust registration.

The persistence half of the validation ledger (docs/VALIDATION_LEDGER.md). Pure parquet I/O; the
comparison/grading logic lives in ``validate.py``. Layout under a DEDICATED validation root (kept
separate from the feature store root so ``store.drop_before``'s ``date=`` retention sweep can never
silently eat the trust history):

  <root>/exceptions/date=<d>/data.parquet   non-matching cells (mismatch | extra_live)        — Layer 1
  <root>/cell/date=<d>/data.parquet         per (day, feature, symbol) rollup, drill-down       — Layer 2
  <root>/feature_day/data.parquet           per (version, feature, day) — DURABLE trust source  — Layer 3a
  <root>/feature_trust/data.parquet         per (version, feature) trust grade — DURABLE        — Layer 3b

Trust (``feature_trust``) is a PURE RECOMPUTE over ``feature_day`` (group by version, feature), so it
is inherently idempotent and self-healing: re-validating a day overwrites that day's ``feature_day``
rows and the next recompute reflects it — no subtract-then-add bookkeeping, no double-fold guard
needed. ``feature_day`` and ``feature_trust`` are tiny (one row per feature per validated day / per
feature) and retention-exempt; the date-partitioned ``exceptions``/``cell`` layers may be retained.
"""
from __future__ import annotations

import os
from pathlib import Path

import polars as pl

EXCEPTIONS = "exceptions"
CELL = "cell"
FEATURE_DAY = "feature_day"
FEATURE_TRUST = "feature_trust"

# feature_day grain key — re-validating a day replaces exactly these rows (idempotent upsert).
FEATURE_DAY_KEYS = ("version", "feature", "day")


def _atomic_write(frame: pl.DataFrame, target: Path) -> Path:
    """Write parquet via a same-dir temp + atomic rename, so a concurrent reader never sees a partial
    file and a re-run overwrites cleanly (mirrors store.write_group)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{target.name}.{os.getpid()}"
    frame.write_parquet(tmp, compression="zstd", compression_level=19)
    os.replace(tmp, target)
    return target


def write_exceptions(root: str | Path, day: str, frame: pl.DataFrame) -> Path:
    """Layer 1: the non-matching cells for a day (mismatch + extra_live). Idempotent per day."""
    return _atomic_write(frame, Path(root) / EXCEPTIONS / f"date={day}" / "data.parquet")


def write_cell(root: str | Path, day: str, frame: pl.DataFrame) -> Path:
    """Layer 2: the per-(feature, symbol) rollup for a day. Idempotent per day."""
    return _atomic_write(frame, Path(root) / CELL / f"date={day}" / "data.parquet")


def read_cell(root: str | Path, day: str) -> pl.DataFrame:
    path = Path(root) / CELL / f"date={day}" / "data.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def read_exceptions(root: str | Path, day: str) -> pl.DataFrame:
    path = Path(root) / EXCEPTIONS / f"date={day}" / "data.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def read_feature_day(root: str | Path) -> pl.DataFrame:
    path = Path(root) / FEATURE_DAY / "data.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def upsert_feature_day(root: str | Path, rows: pl.DataFrame) -> Path:
    """Replace the (version, feature, day) rows present in ``rows`` and keep all others — so
    re-validating a day overwrites its contribution exactly once, with no double counting. ``rows`` is
    the durable per-feature-per-day trust source aggregated across symbols."""
    if rows.height == 0:
        return Path(root) / FEATURE_DAY / "data.parquet"
    existing = read_feature_day(root)
    if existing.height:
        existing = existing.select(rows.columns)  # align column order before anti-join + concat
        kept = existing.join(rows.select(FEATURE_DAY_KEYS), on=list(FEATURE_DAY_KEYS), how="anti")
        merged = pl.concat([kept, rows])
    else:
        merged = rows
    return _atomic_write(merged.sort(FEATURE_DAY_KEYS), Path(root) / FEATURE_DAY / "data.parquet")


def write_trust(root: str | Path, frame: pl.DataFrame) -> Path:
    """Layer 3b: the per-(version, feature) trust registration — recomputed from feature_day."""
    return _atomic_write(frame, Path(root) / FEATURE_TRUST / "data.parquet")


def read_trust(root: str | Path) -> pl.DataFrame:
    path = Path(root) / FEATURE_TRUST / "data.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


# Grade ordering for the training gate: A is best, U (unvalidated) is worst.
_GRADE_RANK: dict[str, int] = {"A": 4, "B": 3, "C": 2, "F": 1, "U": 0}


def certified_features(
    root: str | Path,
    min_value_grade: str = "B",
    min_coverage_grade: str = "B",
    allowed_status: tuple[str, ...] = ("certified",),
) -> set[str]:
    """The set of feature names whose real-time collection is PROVEN to reproduce backfill well enough
    to train on — status in ``allowed_status`` AND both grades at/above the floors. This is the gate a
    training export intersects with so a model never trains on a feature production can't reproduce.
    An empty/missing trust table yields the empty set (nothing is certified until validated)."""
    trust = read_trust(root)
    if trust.height == 0:
        return set()
    value_floor, coverage_floor = _GRADE_RANK[min_value_grade], _GRADE_RANK[min_coverage_grade]
    rank = pl.col("value_grade").replace_strict(_GRADE_RANK, default=0)
    coverage_rank = pl.col("coverage_grade").replace_strict(_GRADE_RANK, default=0)
    keep = trust.filter(
        pl.col("status").is_in(allowed_status) & (rank >= value_floor) & (coverage_rank >= coverage_floor)
    )
    return set(keep["feature"].to_list())


def untrusted_among(
    requested: list[str], root: str | Path, min_value_grade: str = "B", min_coverage_grade: str = "B"
) -> set[str]:
    """Of ``requested`` features, those NOT certified — what a training gate must refuse (or drop)."""
    return set(requested) - certified_features(root, min_value_grade, min_coverage_grade)
