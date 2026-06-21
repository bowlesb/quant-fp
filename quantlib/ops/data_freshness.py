"""Freshness alert for the two non-tape data sources — SEC EDGAR filings + Alpaca news.

Closes the monitoring blind spot documented in ``docs/NEWS_EDGAR_DATA_PIPELINE.md`` (G1/G2): the
``quant-edgar-1`` poll log emits ``poll: 100 filings upserted`` on every cycle because
``upsert_filings`` returns the parsed *feed size*, not rows genuinely inserted. A real ingest stall
(feed frozen, DB write failing silently, account/IP block) therefore logs **identically** to health,
exactly as the fc 06-17 outage looked Up-but-not-ingesting. fc capture has ``live_monitor`` watching its
newest minute; the EDGAR + news ingesters had no equivalent.

This module computes the TRUE newest ingest instant for each source and grades staleness against a
**market-hours-aware** threshold, so the legitimate weekend/overnight SEC lull (SEC publishes almost
nothing Fri-night → Sun) does not page:

- **EDGAR:** newest ``discovered_at`` for ``source='stream'`` rows in the ``filings`` table (our
  wall-clock receipt of a live filing — the real ingest signal, NOT the misleading log line). Alerts
  only when stale beyond a threshold *during SEC business hours* (Mon-Fri, daytime ET); off-hours and
  weekends are expected lulls and never alert.
- **News:** newest ``available_at`` for ``available_at_source='live_arrival'`` rows in ``/store/news``.
  The news feed runs 24/7, but article volume collapses overnight/weekends, so the same business-hours
  gating applies before alerting (a quiet overnight is not a stall).

It is READ-ONLY: a DB SELECT + a polars scan of the newest few news partitions (memory-bounded — never
the whole dataset). It never touches the live ingesters. Run it inside any container that has DB creds +
the ``/store`` mount (fp-dev / feature-computer / quant-dashboard-1 / quant-edgar-1); the proposed cron
execs it in quant-dashboard-1, mirroring the store-glimpse collector.

Exit code: 0 when no source is STALE, 1 when at least one source is STALE (so a cron wrapper can detect
failure). WARN/STALE are logged and a single JSON status line is appended for the audit trail.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

import polars as pl
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [data_freshness] %(message)s",
)
logger = logging.getLogger("data_freshness")

ET = ZoneInfo("America/New_York")
STORE_ROOT = os.environ.get("STORE_ROOT", "/store")
NEWS_GLOB = STORE_ROOT + "/news/published_date=*/data.parquet"
SRC_LIVE = "live_arrival"  # mirrors quantlib.data.news_store.SRC_LIVE (kept local to avoid the heavy import)

# SEC EDGAR dissemination roughly tracks the business day. The current-filings Atom feed is busy across
# the extended session and into the early evening; it goes quiet overnight and on weekends. We alert on
# staleness ONLY inside this business-hours window so a legitimate lull never pages.
EDGAR_BUSINESS_START_MINUTE = 6 * 60  # 06:00 ET
EDGAR_BUSINESS_END_MINUTE = 22 * 60  # 22:00 ET (filings keep landing into the evening — see the audit)

# Staleness thresholds, in minutes, applied ONLY during business hours. Outside business hours / on
# weekends, a source is reported INACTIVE (expected lull), never STALE.
EDGAR_STALE_WARN_MIN = float(os.environ.get("EDGAR_STALE_WARN_MIN", "45"))
EDGAR_STALE_FAIL_MIN = float(os.environ.get("EDGAR_STALE_FAIL_MIN", "120"))
NEWS_STALE_WARN_MIN = float(os.environ.get("NEWS_STALE_WARN_MIN", "90"))
NEWS_STALE_FAIL_MIN = float(os.environ.get("NEWS_STALE_FAIL_MIN", "240"))

# News partitions are UTC-date directories. We only need the newest live arrival, so scan a small trailing
# window of partitions rather than all ~187 (memory-bounded, fast).
NEWS_SCAN_PARTITIONS = int(os.environ.get("NEWS_SCAN_PARTITIONS", "4"))


class Status(str, Enum):
    OK = "OK"
    WARN = "WARN"
    STALE = "STALE"
    INACTIVE = "INACTIVE"  # outside business hours / expected lull — not graded as a failure
    ERROR = "ERROR"  # the probe itself could not read the source


@dataclass
class FreshnessResult:
    source: str
    status: Status
    detail: str
    newest_iso: str | None = None
    age_minutes: float | None = None

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        record["status"] = self.status.value
        return record


def now_utc(reference: datetime | None = None) -> datetime:
    return reference if reference is not None else datetime.now(tz=timezone.utc)


def in_business_hours(reference: datetime) -> bool:
    """True if ``reference`` is within SEC business hours (Mon-Fri, ~06:00-22:00 ET).

    Pure given its input so the gating is unit-testable without a clock.
    """
    et_moment = reference.astimezone(ET)
    if et_moment.weekday() >= 5:  # Sat/Sun
        return False
    minute_of_day = et_moment.hour * 60 + et_moment.minute
    return EDGAR_BUSINESS_START_MINUTE <= minute_of_day < EDGAR_BUSINESS_END_MINUTE


def grade_age(
    source: str,
    newest: datetime | None,
    reference: datetime,
    warn_min: float,
    fail_min: float,
) -> FreshnessResult:
    """Grade a source's newest-ingest age against the thresholds, business-hours-aware.

    ``newest`` None => the source has never ingested (ERROR if in business hours, else INACTIVE).
    Outside business hours any age is INACTIVE (a quiet lull is expected, never a failure).
    """
    business = in_business_hours(reference)
    if newest is None:
        if business:
            return FreshnessResult(source, Status.ERROR, "no ingested rows found")
        return FreshnessResult(source, Status.INACTIVE, "no ingested rows found (off-hours)")

    newest_utc = newest.astimezone(timezone.utc)
    age_minutes = (reference - newest_utc).total_seconds() / 60.0
    newest_iso = newest_utc.isoformat()
    detail = f"newest ingest {newest_iso} is {age_minutes:.0f} min old"

    if not business:
        return FreshnessResult(
            source,
            Status.INACTIVE,
            detail + " — off business-hours (lull expected, not graded)",
            newest_iso,
            age_minutes,
        )
    if age_minutes >= fail_min:
        return FreshnessResult(
            source,
            Status.STALE,
            detail + f" — STALE (>{fail_min:.0f} min during business hours; ingest likely stalled)",
            newest_iso,
            age_minutes,
        )
    if age_minutes >= warn_min:
        return FreshnessResult(
            source,
            Status.WARN,
            detail + f" — lagging (>{warn_min:.0f} min during business hours)",
            newest_iso,
            age_minutes,
        )
    return FreshnessResult(source, Status.OK, detail, newest_iso, age_minutes)


def db_connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=8,
    )


def newest_edgar_ingest(conn: psycopg.Connection) -> datetime | None:
    """Max ``discovered_at`` for live stream filings — our true wall-clock receipt of a live filing.

    This is the real freshness signal the misleading ``poll: 100 filings upserted`` log can't provide.
    ``source='stream'`` excludes the backfill rows, whose ``discovered_at`` reflects when a backfill ran,
    not live ingest.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(discovered_at) FROM filings WHERE source = 'stream'")
        row = cur.fetchone()
    if row is None:
        return None
    newest = row[0]
    if newest is not None and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return newest


def recent_news_partition_paths(limit: int) -> list[str]:
    """The ``limit`` newest news partition parquet paths (by published_date), newest first."""
    paths = glob.glob(NEWS_GLOB)
    paths.sort(reverse=True)
    return paths[:limit]


def newest_news_ingest(limit: int = NEWS_SCAN_PARTITIONS) -> datetime | None:
    """Max ``available_at`` among live-arrival rows in the newest few news partitions.

    Scans only a trailing window of partitions (memory-bounded). Filters to ``available_at_source ==
    SRC_LIVE`` so backfilled (``alpaca_created``) rows — whose ``available_at`` is the historical publish
    instant, not our ingest time — never mask a live-feed stall.
    """
    paths = recent_news_partition_paths(limit)
    if not paths:
        return None
    lazy = (
        pl.scan_parquet(paths)
        .filter(pl.col("available_at_source") == SRC_LIVE)
        .select(pl.col("available_at").max())
    )
    newest = lazy.collect().item()
    if newest is None:
        return None
    if isinstance(newest, datetime) and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return newest


def check_edgar(reference: datetime) -> FreshnessResult:
    try:
        with db_connect() as conn:
            newest = newest_edgar_ingest(conn)
    except psycopg.OperationalError as exc:
        return FreshnessResult("edgar", Status.ERROR, f"DB unreachable: {exc}")
    return grade_age("edgar", newest, reference, EDGAR_STALE_WARN_MIN, EDGAR_STALE_FAIL_MIN)


def check_news(reference: datetime) -> FreshnessResult:
    newest = newest_news_ingest()
    return grade_age("news", newest, reference, NEWS_STALE_WARN_MIN, NEWS_STALE_FAIL_MIN)


def run_checks(reference: datetime | None = None) -> list[FreshnessResult]:
    moment = now_utc(reference)
    return [check_edgar(moment), check_news(moment)]


def log_results(results: list[FreshnessResult]) -> None:
    for result in results:
        message = f"{result.source}: {result.status.value} — {result.detail}"
        if result.status in (Status.STALE, Status.ERROR):
            logger.warning(message)
        elif result.status == Status.WARN:
            logger.warning(message)
        else:
            logger.info(message)


def status_line(results: list[FreshnessResult], reference: datetime) -> str:
    record: dict[str, object] = {"ts": reference.astimezone(timezone.utc).isoformat()}
    for result in results:
        record[result.source] = result.to_dict()
    return json.dumps(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the JSON status line to stdout (for the cron audit trail)",
    )
    args = parser.parse_args(argv)

    reference = now_utc()
    results = run_checks(reference)
    log_results(results)
    if args.json:
        print(status_line(results, reference))

    stale = [result for result in results if result.status == Status.STALE]
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
