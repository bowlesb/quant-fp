"""Persist the validation ledger's records into Postgres (db/init/07_feature_validation.sql).

The DB is the canonical record store (docs/VALIDATION_LEDGER.md §3): small, relational, queryable
verification records — the per-(feature,day) rollup, the per-feature trust registration the training
gate reads, and the rare diverging cells. `validate.py` calls `write_validation(...)` with the frames
it already computes over the parquet feature store.

Row construction is factored into pure `_rows_*` helpers (unit-tested without a DB); only the thin
`write_validation` actually touches psycopg. UPSERT semantics make a re-validation of a day idempotent:
the day's rollup/exceptions are replaced, and `feature_trust` is a full overwrite of the recompute.
"""
from __future__ import annotations

import os

import polars as pl
import psycopg

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

_DAY_COLUMNS = ("feature", "version", "day", "tier", "method", "n_compared", "n_match", "n_mismatch",
                "n_extra_live", "n_missing_live", "value_rate", "coverage_rate", "worst_abs_err")
_TRUST_COLUMNS = ("feature", "version", "status", "value_grade", "coverage_grade", "method",
                  "n_days_validated", "lifetime_compared", "lifetime_match", "lifetime_value_rate",
                  "lifetime_coverage_rate", "last_validated_day", "last_day_value_rate")
_EXC_COLUMNS = ("feature", "symbol", "ts", "day", "tier", "status", "stream_value", "backfill_value",
                "abs_err", "rel_err")


def _rows_day(feature_day: pl.DataFrame) -> list[tuple]:
    """feature_validation_day rows. validate.py's feature_day is aggregated across tiers -> tier 0
    (the all-tiers rollup the schema reserves)."""
    if feature_day.height == 0:
        return []
    frame = feature_day.with_columns(pl.lit(0).cast(pl.Int32).alias("tier"))
    return list(frame.select(_DAY_COLUMNS).iter_rows())


def _rows_trust(trust: pl.DataFrame) -> list[tuple]:
    if trust.height == 0:
        return []
    return list(trust.select(_TRUST_COLUMNS).iter_rows())


def _rows_exceptions(exceptions: pl.DataFrame) -> list[tuple]:
    """feature_validation_exception rows; the ledger's per-cell `minute` becomes the table's `ts`."""
    if exceptions.height == 0:
        return []
    frame = exceptions.rename({"minute": "ts"})
    return list(frame.select(_EXC_COLUMNS).iter_rows())


_UPSERT_DAY = """
INSERT INTO feature_validation_day
  (feature,version,day,tier,method,n_compared,n_match,n_mismatch,n_extra_live,n_missing_live,
   value_rate,coverage_rate,worst_abs_err)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (feature,version,day,tier) DO UPDATE SET
  method=EXCLUDED.method, n_compared=EXCLUDED.n_compared, n_match=EXCLUDED.n_match,
  n_mismatch=EXCLUDED.n_mismatch, n_extra_live=EXCLUDED.n_extra_live,
  n_missing_live=EXCLUDED.n_missing_live, value_rate=EXCLUDED.value_rate,
  coverage_rate=EXCLUDED.coverage_rate, worst_abs_err=EXCLUDED.worst_abs_err, validated_at=now()
"""

_UPSERT_TRUST = """
INSERT INTO feature_trust
  (feature,version,status,value_grade,coverage_grade,method,n_days_validated,lifetime_compared,
   lifetime_match,lifetime_value_rate,lifetime_coverage_rate,last_validated_day,last_day_value_rate)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (feature,version) DO UPDATE SET
  status=EXCLUDED.status, value_grade=EXCLUDED.value_grade, coverage_grade=EXCLUDED.coverage_grade,
  method=EXCLUDED.method, n_days_validated=EXCLUDED.n_days_validated,
  lifetime_compared=EXCLUDED.lifetime_compared, lifetime_match=EXCLUDED.lifetime_match,
  lifetime_value_rate=EXCLUDED.lifetime_value_rate,
  lifetime_coverage_rate=EXCLUDED.lifetime_coverage_rate,
  last_validated_day=EXCLUDED.last_validated_day, last_day_value_rate=EXCLUDED.last_day_value_rate,
  updated_at=now()
"""

_INSERT_EXC = """
INSERT INTO feature_validation_exception
  (feature,symbol,ts,day,tier,status,stream_value,backfill_value,abs_err,rel_err)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (feature,symbol,ts) DO UPDATE SET
  day=EXCLUDED.day, tier=EXCLUDED.tier, status=EXCLUDED.status, stream_value=EXCLUDED.stream_value,
  backfill_value=EXCLUDED.backfill_value, abs_err=EXCLUDED.abs_err, rel_err=EXCLUDED.rel_err,
  recorded_at=now()
"""


def write_validation(
    feature_day: pl.DataFrame, trust: pl.DataFrame, exceptions: pl.DataFrame, day: str
) -> None:
    """UPSERT a day's validation into Postgres. The day's stale exceptions are cleared first (a
    re-validation may have FEWER mismatches), then re-inserted, so the audit trail never keeps a cell
    that has since been fixed."""
    day_rows, trust_rows, exc_rows = _rows_day(feature_day), _rows_trust(trust), _rows_exceptions(exceptions)
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if day_rows:
            cur.executemany(_UPSERT_DAY, day_rows)
        if trust_rows:
            cur.executemany(_UPSERT_TRUST, trust_rows)
        cur.execute("DELETE FROM feature_validation_exception WHERE day = %s", (day,))
        if exc_rows:
            cur.executemany(_INSERT_EXC, exc_rows)
        conn.commit()
