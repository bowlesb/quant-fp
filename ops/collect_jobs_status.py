#!/usr/bin/env python3
"""Host-side READ-ONLY collector for the /jobs dashboard page.

Runs on the HOST (where ``crontab -l``, the ``~/.quant-*`` logs, and ``docker ps`` are reachable; the
dashboard container sees none of these). It aggregates the production cron registry, the live docker jobs,
and recent runs into a single JSON file (``~/.quant-ops/jobs_status.json``) that the dashboard mounts and
the ``/jobs`` page renders — mirroring the status-dashboard pattern (status_store.py + /status).

It only READS: ``crontab -l``, log files under ``~/.quant-*``, and ``docker ps``. It never mutates the
crontab, the logs, or any container. A missing/unreadable log degrades that job's status to ``unknown``;
the collector never crashes on a single bad input.

Schema (``jobs_status.json``)::

    {
      "scheduled": [{name, schedule, purpose, last_run, status, log}],
      "running":   [{name, status}],
      "recent_runs": [{ts, job, status}],   # newest first, ~10
      "collected_at": "2026-06-18T..Z"
    }

``status`` is one of ``ok`` / ``failed`` / ``stale`` / ``unknown``:
  - ``ok``     — the log's recent tail shows a success marker and was written recently enough.
  - ``failed`` — the tail shows an error/traceback/non-zero-exit marker.
  - ``stale``  — the log exists but has not been touched within the job's freshness budget (a due run is
                 missing), and no explicit failure marker is present.
  - ``unknown``— the log is missing/empty/unreadable, so nothing can be inferred.

The cron registry below is the parsing source of truth and must stay in sync with the
``docs/OPERATIONS.md`` §Registry table. Each entry pins the job to its own ``~/.quant-*`` verify-log (the
registry's Verify column) and a freshness budget used for the stale check.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()

JOBS_STATUS_PATH = Path(
    os.environ.get("JOBS_STATUS_PATH", str(HOME / ".quant-ops" / "jobs_status.json"))
)

# Crash signatures that classify a log tail as ``failed``. A failure marker anywhere in the recent tail wins
# over a success marker, because a job can print progress then crash. These are CRASH signatures, not the
# bare words "fail"/"error" — structured logs (the healthcheck PASS/WARN/FAIL summary, the feature_scan
# JSON) legitimately contain those words as DATA, so matching them would mis-flag a clean run.
FAILURE_MARKERS = (
    "traceback (most recent call last)",
    "valueerror",
    "keyerror",
    "typeerror",
    "runtimeerror",
    "command not found",
    "no such file",
    "exit code 1",
    "non-zero exit",
    "fatal:",
)
# Per-job success markers are defined on each registry entry; these are generic fallbacks.
GENERIC_SUCCESS_MARKERS = ("complete", "done", "pass", '"ts"')

# How many trailing lines of a log we scan for status markers.
TAIL_LINES = 40

# Containers we treat as ad-hoc "jobs" (backfills / sweeps / dev runs), matched as substrings of the
# container name. The long-lived service containers (timescaledb, redis, the strategies, the dashboard
# itself) are NOT jobs and are excluded.
RUNNING_JOB_NAME_PARTS = (
    "-bf",
    "backfill",
    "sweep",
    "fp-dev",
    "oflow",
    "materialize",
    "raw-bf",
)


@dataclass(frozen=True)
class CronJob:
    """One production cron from the docs/OPERATIONS.md registry.

    ``cron_match`` is a substring that identifies this job's line in ``crontab -l`` (so we report whether it
    is actually installed). ``log`` is the registry Verify-column path. ``freshness`` is how recently the log
    must have been written for a scheduled run to count as fresh; older than that with a due run pending →
    ``stale``. ``success_markers`` are job-specific phrases that mean a clean run.
    """

    name: str
    schedule: str
    purpose: str
    cron_match: str
    log: Path
    freshness: dt.timedelta
    success_markers: tuple[str, ...]


VALIDATION = HOME / ".quant-validation"
HEALTHCHECK = HOME / ".quant-healthcheck"
OPS = HOME / ".quant-ops"

# Mirrors docs/OPERATIONS.md §Registry. Keep in sync when a cron is added/changed/removed.
REGISTRY: list[CronJob] = [
    CronJob(
        name="healthcheck",
        schedule="*/5 * * * * (PT)",
        purpose="fc freshness/health logging",
        cron_match="ops/healthcheck.sh",
        log=HEALTHCHECK / "healthcheck.jsonl",
        freshness=dt.timedelta(minutes=20),
        # The tripwire's own summary line ("HEALTHCHECK n PASS / n WARN / n FAIL") means it RAN; the
        # PASS/WARN/FAIL counts are its findings, not this job's crash state.
        success_markers=("healthcheck",),
    ),
    CronJob(
        name="feature_scan",
        schedule="2-59/6 * * * * (PT)",
        purpose="per-feature dead/NaN/const/inf sanity scan",
        cron_match="feature_scan",
        log=HEALTHCHECK / "feature_scan.jsonl",
        freshness=dt.timedelta(minutes=20),
        # Each scan emits one JSON object; the presence of its "day" key means the scan ran.
        success_markers=('"day"',),
    ),
    CronJob(
        name="live_monitor",
        schedule="*/3 * * * * (PT)",
        purpose="restart EXITED critical containers; mem/disk guard",
        cron_match="ops/live_monitor.sh",
        # The cron redirects stdout to live_monitor.cron.log, but the monitor's real per-tick record is the
        # JSON line in live_monitor.jsonl (registry Verify column); that file is the freshness signal.
        log=OPS / "live_monitor.jsonl",
        freshness=dt.timedelta(minutes=15),
        success_markers=('"ts"',),
    ),
    CronJob(
        name="nightly_relaunch",
        schedule="11 5 * * 1-5 (PT)",
        purpose="05:11 PT pre-market clean recreate of feature-computer for the session",
        cron_match="ops/nightly_relaunch.sh",
        log=VALIDATION / "nightly_relaunch.log",
        # Weekday-morning job; a day's budget covers "ran this morning".
        freshness=dt.timedelta(hours=30),
        success_markers=("relaunch complete",),
    ),
    CronJob(
        name="daily_lifecycle",
        schedule="30 18 * * 1-5 (PT)",
        purpose="18:30 PT post-close parity sweep + trust ledger",
        cron_match="ops/daily_lifecycle.sh",
        log=VALIDATION / "daily_lifecycle.log",
        freshness=dt.timedelta(hours=30),
        # The chain execs into validation_sweep; a clean run prints the sweep's trust-summary lines, while a
        # crash (e.g. raw not yet settled → ValueError) is caught by FAILURE_MARKERS first. The STAGE 2 banner
        # is the "got through acquire, into the sweep" signal for runs whose summary wording varies.
        success_markers=("trusted", "validation complete", "stage 2/2 sweep"),
    ),
    CronJob(
        name="trust_random_check",
        schedule="45 14 * * 6 (PT)",
        purpose="weekly RANDOM re-check of TRUSTED features; un-trusts clean-day failures",
        cron_match="ops/trust_random_check.sh",
        log=VALIDATION / "trust_random_check.log",
        # Weekly job; one week + slack.
        freshness=dt.timedelta(days=8),
        success_markers=("re-check complete", "no un-trust", "complete"),
    ),
]


def utc_now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_tail(path: Path, n: int = TAIL_LINES) -> list[str]:
    """Last ``n`` non-empty stripped lines of ``path``; empty list if missing/unreadable."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-n:]


def _file_mtime_utc(path: Path) -> dt.datetime | None:
    if not path.exists():
        return None
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)


def classify_log(
    job: CronJob, tail: list[str], mtime: dt.datetime | None, now: dt.datetime
) -> str:
    """Infer ``ok`` / ``failed`` / ``stale`` / ``unknown`` for one cron from its log tail + mtime."""
    if not tail or mtime is None:
        return "unknown"
    blob = "\n".join(tail).lower()
    if any(marker in blob for marker in FAILURE_MARKERS):
        return "failed"
    success_markers = job.success_markers or GENERIC_SUCCESS_MARKERS
    if any(marker.lower() in blob for marker in success_markers):
        # A clean marker, but only "ok" if it is also recent enough; an old success with a missed run is
        # stale (the job stopped running), which is the more useful signal.
        if now - mtime <= job.freshness:
            return "ok"
        return "stale"
    # No explicit marker either way: lean on freshness.
    if now - mtime > job.freshness:
        return "stale"
    return "unknown"


def crontab_lines() -> list[str]:
    """Live ``crontab -l`` lines; empty list if no crontab or the command is unavailable."""
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def is_installed(job: CronJob, crontab: list[str]) -> bool:
    """True if a NON-commented crontab line contains the job's match substring."""
    for line in crontab:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if job.cron_match in stripped:
            return True
    return False


def collect_scheduled(
    crontab: list[str], now: dt.datetime
) -> list[dict[str, str | None]]:
    """One entry per registry cron, with installed-state-aware status + last-run from its log mtime."""
    scheduled: list[dict[str, str | None]] = []
    for job in REGISTRY:
        mtime = _file_mtime_utc(job.log)
        tail = _read_tail(job.log)
        installed = is_installed(job, crontab)
        if not installed:
            # Documented but not in the live crontab → it cannot be running; flag rather than infer "ok".
            status = "stale"
        else:
            status = classify_log(job, tail, mtime, now)
        scheduled.append(
            {
                "name": job.name,
                "schedule": job.schedule + ("" if installed else " [NOT INSTALLED]"),
                "purpose": job.purpose,
                "last_run": (
                    mtime.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    if mtime
                    else None
                ),
                "status": status,
                "log": str(job.log),
            }
        )
    return scheduled


_PS_FORMAT = "{{.Names}}\t{{.Status}}"


def collect_running() -> list[dict[str, str]]:
    """Ad-hoc docker JOB containers (backfills/sweeps/dev runs) — name + status. Read-only ``docker ps``."""
    result = subprocess.run(
        ["docker", "ps", "--format", _PS_FORMAT],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return []
    running: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        name = name.strip()
        if any(part in name for part in RUNNING_JOB_NAME_PARTS):
            running.append({"name": name, "status": status.strip()})
    return running


# Leading ISO-8601 / "YYYY-MM-DDTHH:MM" timestamp some logs prefix their lines with.
_TS_PREFIX = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?Z?)")


def _line_status(line: str) -> str:
    lower = line.lower()
    if any(marker in lower for marker in FAILURE_MARKERS):
        return "failed"
    if any(marker in lower for marker in GENERIC_SUCCESS_MARKERS):
        return "ok"
    return "unknown"


def collect_recent_runs(now: dt.datetime, limit: int = 10) -> list[dict[str, str]]:
    """Newest-first recent run events across jobs, taken from each registry log's mtime + last status line.

    One representative event per job (its most recent run), classified by the log tail and timestamped by the
    log's mtime. This is a coarse roll-up — enough for the page's "Recent Runs" strip without parsing every
    historical run out of multi-MB logs.
    """
    events: list[tuple[dt.datetime, dict[str, str]]] = []
    for job in REGISTRY:
        mtime = _file_mtime_utc(job.log)
        if mtime is None:
            continue
        tail = _read_tail(job.log)
        status = classify_log(job, tail, mtime, now)
        events.append(
            (
                mtime,
                {
                    "ts": mtime.replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "job": job.name,
                    "status": status,
                },
            )
        )
    events.sort(key=lambda pair: pair[0], reverse=True)
    return [event for _, event in events[:limit]]


def collect() -> dict[str, object]:
    now = dt.datetime.now(dt.timezone.utc)
    crontab = crontab_lines()
    return {
        "scheduled": collect_scheduled(crontab, now),
        "running": collect_running(),
        "recent_runs": collect_recent_runs(now),
        "collected_at": utc_now_iso(),
    }


def write_status(data: dict[str, object]) -> None:
    """Atomic replace so a reader never sees a half-written file."""
    JOBS_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_STATUS_PATH.with_suffix(JOBS_STATUS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, JOBS_STATUS_PATH)


def main() -> None:
    data = collect()
    write_status(data)
    print(
        f"collect_jobs_status: wrote {JOBS_STATUS_PATH} "
        f"({len(data['scheduled'])} scheduled, {len(data['running'])} running)"  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    main()
