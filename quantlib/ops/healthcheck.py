"""Continuous health-check for the live feature-collection platform.

Runs INSIDE the feature-computer/fp-dev container (polars + DB network + the /store mount). Each check
is a module-level function returning a structured ``CheckResult``; the registry dispatches them with
fault isolation so one failing check can never abort the run. The host wrapper (``ops/healthcheck.sh``)
does the docker-level checks this process cannot, then execs ``python -m quantlib.ops.healthcheck --json``.

This is the engine of the "always look, continuously, then fix" protocol: every check computes a REAL
number from the live system (newest-minute age, distinct-symbol coverage, NaN rates, Prometheus
latency, parity/trust grades, disk) and emits a concise human detail plus an optional fix hint.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

import polars as pl
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [healthcheck] %(message)s",
)
logger = logging.getLogger("healthcheck")

ET = ZoneInfo("America/New_York")
STORE_ROOT = os.environ.get("STORE_ROOT", "/store")
STREAM_GLOB_TEMPLATE = (
    STORE_ROOT + "/group={group}/v=*/source=stream/date={day}/*.parquet"
)
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://quant-prometheus-1:9090")
EXPECTED_UNIVERSE_SIZE = int(os.environ.get("EXPECTED_UNIVERSE_SIZE", "11336"))
UNIVERSE_DRIFT_TOLERANCE = float(os.environ.get("UNIVERSE_DRIFT_TOLERANCE", "0.05"))
EXPECTED_CAPTURE_SHARDS = 8

# ET minutes-of-day for the regular session (mirrors quantlib.features.session).
OPEN_MINUTE = 570  # 09:30 ET
CLOSE_MINUTE = 960  # 16:00 ET
PREMARKET_START_MINUTE = 240  # 04:00 ET
AFTERHOURS_END_MINUTE = 1200  # 20:00 ET
MID_SESSION_MINUTE = 720  # 12:00 ET — coverage should be broad by here

# Latency thresholds (seconds).
VECTOR_LATENCY_WARN_S = 0.5
VECTOR_LATENCY_FAIL_S = 2.0
# Our-compute thresholds (feature_assemble_seconds = last-bar -> ready, excludes Alpaca delivery lag).
COMPUTE_LATENCY_WARN_S = float(os.environ.get("COMPUTE_LATENCY_WARN_S", "1.0"))
COMPUTE_LATENCY_FAIL_S = float(os.environ.get("COMPUTE_LATENCY_FAIL_S", "5.0"))

# A-E share of an unbiased US-equity ticker distribution sits ~30-35%; a hard alphabetical [:N] cut
# pushes it far higher, so we flag well above the natural band.
ALPHABETICAL_BIAS_WARN_SHARE = 0.55

# Per-minute active-symbol bands by phase (lo, hi).
PER_MINUTE_BANDS: dict[str, tuple[int, int]] = {
    "premarket": (50, 2500),
    "rth": (1500, 8000),
    "afterhours": (20, 4000),
    "closed": (0, 9000),
}


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    metric: float | None = None
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        record["status"] = self.status.value
        return record


@dataclass
class Phase:
    """Resolved session phase used to adapt coverage thresholds."""

    name: str
    et_minute_of_day: int


def now_et() -> datetime:
    return datetime.now(tz=ET)


def et_today(reference: datetime | None = None) -> date:
    """The ET calendar date used for the ``date=`` store partition."""
    moment = reference if reference is not None else now_et()
    return moment.astimezone(ET).date()


def detect_phase(reference: datetime | None = None) -> Phase:
    """Auto-detect the session phase from the current ET wall-clock time.

    Pure given its input so the phase-threshold logic is unit-testable without a clock.
    """
    moment = reference if reference is not None else now_et()
    et_moment = moment.astimezone(ET)
    minute_of_day = et_moment.hour * 60 + et_moment.minute
    weekday = et_moment.weekday()  # 0=Mon .. 6=Sun
    if weekday >= 5:
        return Phase("closed", minute_of_day)
    if minute_of_day < PREMARKET_START_MINUTE:
        return Phase("closed", minute_of_day)
    if minute_of_day < OPEN_MINUTE:
        return Phase("premarket", minute_of_day)
    if minute_of_day < CLOSE_MINUTE:
        return Phase("rth", minute_of_day)
    if minute_of_day < AFTERHOURS_END_MINUTE:
        return Phase("afterhours", minute_of_day)
    return Phase("closed", minute_of_day)


def resolve_phase(requested: str, reference: datetime | None = None) -> Phase:
    if requested == "auto":
        return detect_phase(reference)
    moment = reference if reference is not None else now_et()
    et_moment = moment.astimezone(ET)
    return Phase(requested, et_moment.hour * 60 + et_moment.minute)


def db_connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=8,
    )


def prometheus_query(expr: str) -> list[dict[str, object]]:
    """Run an instant PromQL query; returns the result vector (possibly empty)."""
    url = PROMETHEUS_URL + "/api/v1/query?query=" + urllib.parse.quote(expr)
    with urllib.request.urlopen(url, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload["status"] != "success":
        raise RuntimeError(f"prometheus query failed: {payload['status']}")
    return list(payload["data"]["result"])


def prometheus_scalar(expr: str) -> float | None:
    """Single scalar from an instant query, or None when no series matched."""
    result = prometheus_query(expr)
    if not result:
        return None
    value = result[0]["value"]
    if not isinstance(value, list):
        raise RuntimeError("unexpected prometheus value shape")
    raw = value[1]
    parsed = float(raw)
    if parsed != parsed:  # NaN guard from histogram_quantile on empty buckets
        return None
    return parsed


def newest_minute_lazy(group: str, day: date) -> pl.LazyFrame | None:
    """Lazy scan of today's stream partition for ``group``; None if the partition is absent."""
    glob = STREAM_GLOB_TEMPLATE.format(group=group, day=day.isoformat())
    try:
        return pl.scan_parquet(glob, missing_columns="insert")
    except (pl.exceptions.ComputeError, FileNotFoundError):
        return None


def scan_today_recent(group: str, day: date, minutes: int) -> pl.LazyFrame | None:
    """Scan today's stream partition for ``group`` filtered to the last ``minutes`` of data.

    Reads only the recent tail conceptually; we let polars push the predicate down. Returns None when
    the partition does not exist yet (e.g. before the first flush).
    """
    lazy = newest_minute_lazy(group, day)
    if lazy is None:
        return None
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    return lazy.filter(pl.col("minute") >= pl.lit(cutoff))


def check_newest_minute_age() -> CheckResult:
    name = "newest_minute_age"
    day = et_today()
    lazy = newest_minute_lazy("volume", day)
    if lazy is None:
        return CheckResult(
            name,
            Status.FAIL,
            f"no stream partition for {day} (group=volume)",
            fix_hint="confirm live_capture is running and flushing for today",
        )
    newest = lazy.select(pl.col("minute").max()).collect().item()
    if newest is None:
        return CheckResult(
            name, Status.FAIL, f"partition for {day} has no rows", fix_hint="capture stalled at flush"
        )
    newest_utc = newest.replace(tzinfo=timezone.utc) if newest.tzinfo is None else newest
    age_seconds = (datetime.now(tz=timezone.utc) - newest_utc).total_seconds()
    age_minutes = age_seconds / 60.0
    detail = f"newest minute {newest_utc.isoformat()} is {age_minutes:.1f} min old"
    if age_minutes > 8:
        return CheckResult(
            name,
            Status.FAIL,
            detail,
            metric=age_minutes,
            fix_hint="stream is stale >8min: check websocket/restart capture",
        )
    if age_minutes > 3:
        return CheckResult(
            name, Status.WARN, detail, metric=age_minutes, fix_hint="stream lagging; watch closely"
        )
    return CheckResult(name, Status.PASS, detail, metric=age_minutes)


def check_worker_targets_up() -> CheckResult:
    name = "worker_targets_up"
    result = prometheus_query(
        "up{job=\"feature-capture\"}"
    )
    capture_down = [
        series["metric"].get("instance", "?")
        for series in result
        if float(series["value"][1]) != 1.0  # type: ignore[index]
    ]
    capture_up = len(result) - len(capture_down)
    other = prometheus_query("up == 0")
    other_down = [
        series["metric"].get("instance", "?")
        for series in other
        if series["metric"].get("job") != "feature-capture"
    ]
    detail = f"{capture_up}/{EXPECTED_CAPTURE_SHARDS} capture shards UP"
    if capture_down or capture_up < EXPECTED_CAPTURE_SHARDS:
        return CheckResult(
            name,
            Status.FAIL,
            f"{detail}; DOWN={capture_down}",
            metric=float(capture_up),
            fix_hint="a feature-capture shard is down — restart feature-computer",
        )
    if other_down:
        return CheckResult(
            name,
            Status.WARN,
            f"{detail}; other DOWN targets: {other_down}",
            metric=float(capture_up),
            fix_hint="stale prometheus targets (e.g. retired ingestor) — prune scrape config",
        )
    return CheckResult(name, Status.PASS, detail, metric=float(capture_up))


def check_universe_size() -> CheckResult:
    name = "universe_size"
    with db_connect() as conn:
        row = conn.execute("select count(*) from universe_membership").fetchone()
    size = int(row[0]) if row is not None else 0
    detail = f"universe_membership={size} (expected ~{EXPECTED_UNIVERSE_SIZE})"
    drift = size - EXPECTED_UNIVERSE_SIZE
    drift_frac = abs(drift) / EXPECTED_UNIVERSE_SIZE if EXPECTED_UNIVERSE_SIZE else 1.0
    if drift_frac > UNIVERSE_DRIFT_TOLERANCE:
        return CheckResult(
            name,
            Status.WARN,
            f"{detail}, drift={drift:+d} ({drift_frac:.0%} > {UNIVERSE_DRIFT_TOLERANCE:.0%}) — confirm intentional",
            metric=float(size),
            fix_hint="universe drifted vs EXPECTED_UNIVERSE_SIZE beyond tolerance — confirm intentional re-seed",
        )
    return CheckResult(name, Status.PASS, detail, metric=float(size))


def distinct_symbols_today(day: date) -> tuple[int, list[str]]:
    """Distinct symbols with >=1 bar today, scanning the volume stream partition once."""
    lazy = newest_minute_lazy("volume", day)
    if lazy is None:
        return 0, []
    symbols = (
        lazy.select(pl.col("symbol").unique()).collect().get_column("symbol").to_list()
    )
    return len(symbols), symbols


def check_distinct_symbol_coverage(phase: Phase) -> CheckResult:
    name = "distinct_symbol_coverage"
    day = et_today()
    distinct_count, _symbols = distinct_symbols_today(day)
    universe = EXPECTED_UNIVERSE_SIZE
    with db_connect() as conn:
        row = conn.execute("select count(*) from universe_membership").fetchone()
        if row is not None and int(row[0]) > 0:
            universe = int(row[0])
    pct = (100.0 * distinct_count / universe) if universe else 0.0
    detail = f"{distinct_count} / {universe} ({pct:.1f}%) distinct symbols today"
    if distinct_count == 0:
        if phase.name in ("closed",):
            return CheckResult(name, Status.WARN, detail + " — market closed", metric=pct)
        return CheckResult(
            name,
            Status.FAIL,
            detail,
            metric=pct,
            fix_hint="no symbols captured today — stream down or universe not subscribed",
        )
    if phase.name == "rth" and phase.et_minute_of_day >= MID_SESSION_MINUTE and pct < 60.0:
        return CheckResult(
            name,
            Status.WARN,
            detail + " — below 60% mid-session",
            metric=pct,
            fix_hint="coverage low for RTH — check subscription breadth / alphabetical cut",
        )
    return CheckResult(name, Status.PASS, detail, metric=pct)


def newest_complete_minute_count(day: date) -> tuple[datetime | None, int]:
    """Active-symbol count in the newest minute that is at least one minute old (i.e. complete)."""
    lazy = newest_minute_lazy("volume", day)
    if lazy is None:
        return None, 0
    safe_cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=90)
    complete = lazy.filter(pl.col("minute") <= pl.lit(safe_cutoff))
    newest = complete.select(pl.col("minute").max()).collect().item()
    if newest is None:
        return None, 0
    count = (
        complete.filter(pl.col("minute") == pl.lit(newest))
        .select(pl.len())
        .collect()
        .item()
    )
    return newest, int(count)


def check_per_minute_active(phase: Phase) -> CheckResult:
    name = "per_minute_active"
    day = et_today()
    newest, count = newest_complete_minute_count(day)
    band_lo, band_hi = PER_MINUTE_BANDS[phase.name]
    if newest is None:
        if phase.name == "closed":
            return CheckResult(name, Status.WARN, "no complete minute (market closed)", metric=0.0)
        return CheckResult(
            name,
            Status.FAIL,
            "no complete minute available",
            metric=0.0,
            fix_hint="capture produced no completed minute — stream stalled",
        )
    detail = f"{count} symbols in {newest.isoformat()} (band {band_lo}-{band_hi} for {phase.name})"
    if count < band_lo or count > band_hi:
        return CheckResult(
            name,
            Status.WARN,
            detail + " — outside band",
            metric=float(count),
            fix_hint="active-symbol count outside expected band for phase",
        )
    return CheckResult(name, Status.PASS, detail, metric=float(count))


def check_alphabetical_bias() -> CheckResult:
    name = "alphabetical_bias"
    day = et_today()
    distinct_count, symbols = distinct_symbols_today(day)
    if distinct_count == 0:
        return CheckResult(name, Status.WARN, "no symbols today to assess bias", metric=None)
    a_to_e = [sym for sym in symbols if sym[:1].upper() in ("A", "B", "C", "D", "E")]
    share = len(a_to_e) / distinct_count
    detail = (
        f"A-E share {share * 100:.1f}% ({len(a_to_e)}/{distinct_count}); "
        f"unbiased ~30-35%"
    )
    if share > ALPHABETICAL_BIAS_WARN_SHARE:
        return CheckResult(
            name,
            Status.WARN,
            detail + " — looks like an alphabetical truncation",
            metric=share,
            fix_hint="universe may be an alphabetical [:N] cut — verify subscription selection",
        )
    return CheckResult(name, Status.PASS, detail, metric=share)


def check_ohlc_invariants() -> CheckResult:
    name = "ohlc_invariants"
    day = et_today()
    lazy = scan_today_recent("volume", day, minutes=5)
    if lazy is None:
        return CheckResult(name, Status.WARN, "no volume partition to sanity-check", metric=None)
    frame = lazy.select(["symbol", "minute", "dollar_volume_1m"]).collect()
    if frame.height == 0:
        return CheckResult(name, Status.WARN, "no recent rows to sanity-check", metric=None)
    negatives = frame.filter(pl.col("dollar_volume_1m") < 0)
    n_neg = negatives.height
    detail = f"checked {frame.height} recent rows; {n_neg} negative dollar_volume_1m"
    if n_neg > 0:
        sample = negatives.head(3).to_dicts()
        return CheckResult(
            name,
            Status.FAIL,
            detail + f"; e.g. {sample}",
            metric=float(n_neg),
            fix_hint="negative dollar volume is impossible — data corruption in capture",
        )
    return CheckResult(name, Status.PASS, detail, metric=0.0)


def feature_columns(frame: pl.DataFrame) -> list[str]:
    return [col for col in frame.columns if col not in ("symbol", "minute")]


def check_nan_rates() -> CheckResult:
    name = "nan_rates"
    day = et_today()
    phase = detect_phase()
    # Past-warmup: after the open, all <=90m windows are warm; use rows from after 09:35 ET to avoid
    # flagging the legitimate warmup nulls. Pre-market we cannot judge, so skip to WARN-neutral.
    lazy = newest_minute_lazy("volume", day)
    if lazy is None:
        return CheckResult(name, Status.WARN, "no volume partition for NaN audit", metric=None)
    open_plus = datetime.now(tz=ET).replace(hour=9, minute=35, second=0, microsecond=0)
    open_plus_utc = open_plus.astimezone(timezone.utc)
    if phase.name in ("premarket", "closed"):
        return CheckResult(
            name,
            Status.PASS,
            "skipped: pre-warmup/closed phase (NaN audit needs RTH rows)",
            metric=None,
        )
    frame = lazy.filter(pl.col("minute") >= pl.lit(open_plus_utc)).collect()
    if frame.height == 0:
        return CheckResult(name, Status.WARN, "no post-warmup rows yet for NaN audit", metric=None)
    cols = feature_columns(frame)
    nan_counts = frame.select(
        [pl.col(col).is_null().sum().alias(col) for col in cols]
    ).to_dicts()[0]
    total = frame.height
    rates = {col: nan_counts[col] / total for col in cols}
    worst = sorted(rates.items(), key=lambda item: -item[1])[:5]
    high = [(col, rate) for col, rate in rates.items() if rate > 0.20]
    detail_worst = ", ".join(f"{col}={rate * 100:.0f}%" for col, rate in worst)
    detail = f"worst NaN rates over {total} rows: {detail_worst}"
    if high:
        return CheckResult(
            name,
            Status.WARN,
            detail + f"; {len(high)} features >20% NaN",
            metric=worst[0][1],
            fix_hint="unexpectedly high NaN past warmup — inspect those feature kernels",
        )
    return CheckResult(name, Status.PASS, detail, metric=worst[0][1] if worst else 0.0)


def check_constant_features() -> CheckResult:
    name = "constant_features"
    day = et_today()
    phase = detect_phase()
    if phase.name in ("premarket", "closed"):
        return CheckResult(
            name, Status.PASS, "skipped: pre-warmup/closed phase", metric=None
        )
    lazy = newest_minute_lazy("volume", day)
    if lazy is None:
        return CheckResult(name, Status.WARN, "no volume partition for constancy audit", metric=None)
    open_plus = (
        datetime.now(tz=ET)
        .replace(hour=9, minute=35, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    frame = lazy.filter(pl.col("minute") >= pl.lit(open_plus)).collect()
    if frame.height < 10:
        return CheckResult(name, Status.WARN, "too few rows for constancy audit", metric=None)
    cols = feature_columns(frame)
    nunique = frame.select(
        [pl.col(col).n_unique().alias(col) for col in cols]
    ).to_dicts()[0]
    constant = [col for col in cols if nunique[col] <= 1]
    detail = f"{len(constant)}/{len(cols)} features constant across {frame.height} rows"
    if constant:
        return CheckResult(
            name,
            Status.WARN,
            detail + f": {constant[:8]}",
            metric=float(len(constant)),
            fix_hint="constant feature is likely dead/broken — inspect kernel",
        )
    return CheckResult(name, Status.PASS, detail, metric=0.0)


def latency_quantiles(metric: str, window: str = "15m") -> tuple[float | None, float | None, float | None]:
    """(p50, p95, p99) for a histogram metric, aggregated across shards."""
    quantiles = []
    for q in ("0.50", "0.95", "0.99"):
        quantiles.append(
            prometheus_scalar(f"histogram_quantile({q}, sum(rate({metric}_bucket[{window}])) by (le))")
        )
    return quantiles[0], quantiles[1], quantiles[2]


def check_bar_to_vector_latency() -> CheckResult:
    """Bar -> vector ready latency for the deployed batch path. Both anchors (first-bar end-to-end,
    last-bar assemble) are dominated by the batch minute-close WAIT: a minute's vector cannot be
    assembled until the minute is complete (~T+60s). Pure feature compute is tracked separately by
    group_compute_p99 and is fast — this check surfaces the architectural bet-latency floor, which
    only the per-symbol fast/tick path can break below a minute."""
    name = "bar_to_vector_latency"
    end_p50, _, end_p99 = latency_quantiles("feature_vector_latency_seconds")
    asm_p50, asm_p95, asm_p99 = latency_quantiles("feature_assemble_seconds")
    if asm_p50 is None and end_p50 is None:
        return CheckResult(
            name,
            Status.WARN,
            "latency metrics not emitted (deploy pending)",
            metric=None,
            fix_hint="redeploy capture to start emitting feature_assemble_seconds",
        )
    end_str = f"end2end p50={end_p50 or 0:.1f}s p99={end_p99 or 0:.1f}s"
    asm_str = f"assemble p50={asm_p50 or 0:.1f}s p95={asm_p95 or 0:.1f}s p99={asm_p99 or 0:.1f}s"
    detail = f"{asm_str}; {end_str} (batch path: incl. minute-close wait + Alpaca delivery lag)"
    if asm_p99 is not None and asm_p99 > COMPUTE_LATENCY_FAIL_S:
        return CheckResult(
            name,
            Status.FAIL,
            detail,
            metric=asm_p99,
            fix_hint="batch bar->vector >5s — architectural floor; per-symbol fast path needed for sub-minute bets",
        )
    if asm_p99 is not None and asm_p99 > COMPUTE_LATENCY_WARN_S:
        return CheckResult(name, Status.WARN, detail, metric=asm_p99, fix_hint="bar->vector latency elevated")
    return CheckResult(name, Status.PASS, detail, metric=asm_p99)


def check_group_compute_p99() -> CheckResult:
    name = "group_compute_p99"
    p99 = prometheus_scalar(
        "histogram_quantile(0.99, sum(rate(feature_group_compute_seconds_bucket[15m])) by (le))"
    )
    if p99 is None:
        return CheckResult(
            name, Status.WARN, "feature_group_compute_seconds not emitted", metric=None
        )
    detail = f"feature_group_compute p99={p99:.3f}s over 15m"
    if p99 > VECTOR_LATENCY_FAIL_S:
        return CheckResult(name, Status.WARN, detail + " — slow", metric=p99)
    return CheckResult(name, Status.PASS, detail, metric=p99)


def last_trading_day(reference: date | None = None) -> date:
    """Most recent weekday strictly before ``reference`` (ignores holidays)."""
    cursor = (reference if reference is not None else et_today()) - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def check_validation_freshness() -> CheckResult:
    name = "validation_freshness"
    with db_connect() as conn:
        row = conn.execute("select max(day) from feature_validation_day").fetchone()
    latest = row[0] if row is not None else None
    expected = last_trading_day()
    if latest is None:
        return CheckResult(
            name,
            Status.WARN,
            f"no validated days yet (expected through {expected})",
            metric=None,
            fix_hint="run the validation ledger (validate.py) for the last trading day",
        )
    stale_days = (expected - latest).days
    detail = f"latest validated day {latest}; last trading day {expected} (stale {stale_days}d)"
    if stale_days > 1:
        return CheckResult(
            name,
            Status.WARN,
            detail,
            metric=float(stale_days),
            fix_hint="validation is stale >1 trading day — re-run validate.py",
        )
    return CheckResult(name, Status.PASS, detail, metric=float(max(stale_days, 0)))


def check_trust_grades() -> CheckResult:
    name = "trust_grades"
    with db_connect() as conn:
        rows = conn.execute(
            "select status, count(*) from feature_trust group by status"
        ).fetchall()
        divergent_row = conn.execute(
            "select count(*) from feature_trust where status = 'divergent'"
        ).fetchone()
    status_counts = {str(status): int(count) for status, count in rows}
    total = sum(status_counts.values())
    n_divergent = int(divergent_row[0]) if divergent_row is not None else 0
    if total == 0:
        return CheckResult(
            name,
            Status.WARN,
            "feature_trust empty — no features certified yet",
            metric=0.0,
            fix_hint="no trust registrations — run validation to populate feature_trust",
        )
    detail = f"trust breakdown over {total} features: {status_counts}"
    if n_divergent > 0:
        return CheckResult(
            name,
            Status.WARN,
            detail + f"; {n_divergent} DIVERGENT",
            metric=float(n_divergent),
            fix_hint="features flipped to divergent — parity regression, investigate",
        )
    return CheckResult(name, Status.PASS, detail, metric=float(total))


def check_disk_space() -> CheckResult:
    name = "disk_space"
    mounts = {"/store": STORE_ROOT, "/": "/"}
    details: list[str] = []
    worst_status = Status.PASS
    worst_pct = 100.0
    for label, path in mounts.items():
        usage = shutil.disk_usage(path)
        free_pct = 100.0 * usage.free / usage.total
        details.append(f"{label} {free_pct:.0f}% free")
        if free_pct < worst_pct:
            worst_pct = free_pct
        if free_pct < 5:
            worst_status = Status.FAIL
        elif free_pct < 15 and worst_status != Status.FAIL:
            worst_status = Status.WARN
    detail = "; ".join(details)
    fix_hint = None
    if worst_status != Status.PASS:
        fix_hint = "low disk free — prune old partitions; note host sda is a known failing disk"
    return CheckResult(name, worst_status, detail, metric=worst_pct, fix_hint=fix_hint)


def check_db_growth() -> CheckResult:
    name = "db_growth"
    with db_connect() as conn:
        bars = conn.execute("select count(*) from bars_1m").fetchone()
        validations = conn.execute("select count(*) from feature_validation_day").fetchone()
    n_bars = int(bars[0]) if bars is not None else 0
    n_val = int(validations[0]) if validations is not None else 0
    detail = f"DB reachable; bars_1m={n_bars} rows, feature_validation_day={n_val} rows"
    return CheckResult(name, Status.PASS, detail, metric=float(n_bars))


CheckFn = Callable[[], CheckResult]


def build_registry(phase: Phase) -> list[tuple[str, CheckFn]]:
    """Named checks in run order. Phase-dependent checks are bound to the resolved phase here."""
    return [
        ("newest_minute_age", check_newest_minute_age),
        ("worker_targets_up", check_worker_targets_up),
        ("universe_size", check_universe_size),
        ("distinct_symbol_coverage", lambda: check_distinct_symbol_coverage(phase)),
        ("per_minute_active", lambda: check_per_minute_active(phase)),
        ("alphabetical_bias", check_alphabetical_bias),
        ("ohlc_invariants", check_ohlc_invariants),
        ("nan_rates", check_nan_rates),
        ("constant_features", check_constant_features),
        ("bar_to_vector_latency", check_bar_to_vector_latency),
        ("group_compute_p99", check_group_compute_p99),
        ("validation_freshness", check_validation_freshness),
        ("trust_grades", check_trust_grades),
        ("disk_space", check_disk_space),
        ("db_growth", check_db_growth),
    ]


def run_check(name: str, check_fn: CheckFn) -> CheckResult:
    """Fault-isolated dispatch: a raising check becomes a FAIL result, the run continues."""
    try:
        return check_fn()
    except (
        psycopg.Error,
        urllib.error.URLError,
        pl.exceptions.PolarsError,
        OSError,
        KeyError,
        ValueError,
        RuntimeError,
    ) as error:
        logger.warning("check %s raised %s: %s", name, type(error).__name__, error)
        return CheckResult(
            name,
            Status.FAIL,
            f"check raised {type(error).__name__}: {error}",
            fix_hint="this check errored — inspect the exception",
        )


def run_all(phase: Phase) -> list[CheckResult]:
    return [run_check(name, check_fn) for name, check_fn in build_registry(phase)]


def summarize(results: list[CheckResult]) -> tuple[int, int, int]:
    """(n_pass, n_warn, n_fail)."""
    n_pass = sum(1 for result in results if result.status == Status.PASS)
    n_warn = sum(1 for result in results if result.status == Status.WARN)
    n_fail = sum(1 for result in results if result.status == Status.FAIL)
    return n_pass, n_warn, n_fail


def exit_code_for(results: list[CheckResult]) -> int:
    """0 if no FAIL, 1 if any FAIL. Internal errors (exit 2) are raised by main()."""
    _n_pass, _n_warn, n_fail = summarize(results)
    return 1 if n_fail > 0 else 0


def render_text(results: list[CheckResult], phase: Phase) -> str:
    name_width = max((len(result.name) for result in results), default=4)
    lines = [f"HEALTHCHECK phase={phase.name} (ET minute {phase.et_minute_of_day})"]
    for result in results:
        metric = "" if result.metric is None else f"  [{result.metric:.4g}]"
        lines.append(
            f"  {result.status.value:<4} {result.name:<{name_width}}  {result.detail}{metric}"
        )
    n_pass, n_warn, n_fail = summarize(results)
    lines.append(f"HEALTHCHECK {n_pass} PASS / {n_warn} WARN / {n_fail} FAIL")
    return "\n".join(lines)


def render_json(results: list[CheckResult], phase: Phase) -> str:
    n_pass, n_warn, n_fail = summarize(results)
    payload = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "phase": phase.name,
        "et_minute_of_day": phase.et_minute_of_day,
        "summary": {"pass": n_pass, "warn": n_warn, "fail": n_fail},
        "exit_code": exit_code_for(results),
        "checks": [result.to_dict() for result in results],
    }
    return json.dumps(payload, indent=2, default=str)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live feature-platform health-check")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument(
        "--session-phase",
        choices=["auto", "premarket", "rth", "afterhours", "closed"],
        default="auto",
        help="override the auto-detected ET session phase (affects coverage thresholds)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    phase = resolve_phase(args.session_phase)
    results = run_all(phase)
    if args.json:
        print(render_json(results, phase))
    else:
        print(render_text(results, phase))
    return exit_code_for(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (psycopg.Error, urllib.error.URLError, pl.exceptions.PolarsError, OSError) as fatal:
        logger.error("healthcheck internal error: %s: %s", type(fatal).__name__, fatal)
        sys.exit(2)
