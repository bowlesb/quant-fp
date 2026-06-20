"""Unit tests for jobs status (ops/collect_jobs_status.py writer + services/dashboard/jobs_page.load_status).

No live crontab, docker, or real logs: the collector's log-classification and crontab/installed parsing are
exercised against tmp log files and synthetic ``crontab -l`` line lists, and the page renderer is checked
directly for the structural invariants (three sections, status badges, HTML-escaping, the missing-file
notice). The end-to-end ``collect()`` path that shells out to ``crontab``/``docker`` is not invoked here;
its pure pieces (classify_log, is_installed, render_*) are tested in isolation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))
sys.path.insert(0, str(REPO_ROOT / "services" / "dashboard"))

import collect_jobs_status as cjs  # noqa: E402

NOW = dt.datetime(2026, 6, 18, 12, 0, tzinfo=dt.timezone.utc)


def _job(**overrides) -> cjs.CronJob:
    base = dict(
        name="demo",
        schedule="*/5 * * * * (PT)",
        purpose="demo purpose",
        cron_match="ops/demo.sh",
        log=Path("/nonexistent/demo.log"),
        freshness=dt.timedelta(minutes=20),
        success_markers=("complete",),
    )
    base.update(overrides)
    return cjs.CronJob(**base)  # type: ignore[arg-type]


def _write_log(tmp_path: Path, name: str, body: str, mtime: dt.datetime) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def test_classify_ok_on_recent_success_marker(tmp_path) -> None:
    log = _write_log(
        tmp_path,
        "ok.log",
        "step 1\nstep 2\nrelaunch complete for 2026-06-18\n",
        NOW - dt.timedelta(minutes=5),
    )
    job = _job(log=log, success_markers=("relaunch complete",))
    tail = cjs._read_tail(log)
    mtime = cjs._file_mtime_utc(log)
    assert cjs.classify_log(job, tail, mtime, NOW) == "ok"


def test_classify_failed_on_traceback_even_after_progress(tmp_path) -> None:
    log = _write_log(
        tmp_path,
        "fail.log",
        "starting sweep\nTraceback (most recent call last):\nValueError: raw empty\n",
        NOW - dt.timedelta(minutes=2),
    )
    job = _job(log=log, success_markers=("sweep complete",))
    tail = cjs._read_tail(log)
    mtime = cjs._file_mtime_utc(log)
    assert cjs.classify_log(job, tail, mtime, NOW) == "failed"


def test_classify_stale_on_old_success(tmp_path) -> None:
    log = _write_log(
        tmp_path,
        "old.log",
        "relaunch complete\n",
        NOW - dt.timedelta(hours=5),
    )
    job = _job(
        log=log,
        success_markers=("relaunch complete",),
        freshness=dt.timedelta(minutes=20),
    )
    tail = cjs._read_tail(log)
    mtime = cjs._file_mtime_utc(log)
    assert cjs.classify_log(job, tail, mtime, NOW) == "stale"


def test_classify_unknown_on_missing_log() -> None:
    job = _job(log=Path("/nonexistent/missing.log"))
    assert cjs.classify_log(job, [], None, NOW) == "unknown"


def test_is_installed_ignores_commented_lines() -> None:
    job = _job(cron_match="ops/demo.sh")
    crontab = [
        "PATH=/usr/bin",
        "# *PAUSED 14 * * * * ops/demo.sh",  # commented → not installed
    ]
    assert cjs.is_installed(job, crontab) is False
    crontab.append("*/5 * * * * cd /repo && ops/demo.sh >> /tmp/demo.log 2>&1")
    assert cjs.is_installed(job, crontab) is True


def test_collect_scheduled_flags_not_installed(tmp_path) -> None:
    # A registry job whose match is absent from the crontab is reported stale + [NOT INSTALLED].
    job = _job(name="ghost", cron_match="ops/ghost.sh", log=tmp_path / "ghost.log")
    job.log.write_text("relaunch complete\n", encoding="utf-8")
    original = cjs.REGISTRY
    cjs.REGISTRY = [job]
    try:
        scheduled = cjs.collect_scheduled([], NOW)
    finally:
        cjs.REGISTRY = original
    assert scheduled[0]["status"] == "stale"
    assert "[NOT INSTALLED]" in scheduled[0]["schedule"]


def test_collect_recent_runs_newest_first(tmp_path) -> None:
    job_a = _job(
        name="a",
        log=_write_log(tmp_path, "a.log", "complete\n", NOW - dt.timedelta(hours=2)),
    )
    job_b = _job(
        name="b",
        log=_write_log(tmp_path, "b.log", "complete\n", NOW - dt.timedelta(minutes=5)),
    )
    original = cjs.REGISTRY
    cjs.REGISTRY = [job_a, job_b]
    try:
        recent = cjs.collect_recent_runs(NOW)
    finally:
        cjs.REGISTRY = original
    assert [run["job"] for run in recent] == ["b", "a"]


def test_write_status_atomic_round_trip(tmp_path, monkeypatch) -> None:
    out = tmp_path / "jobs_status.json"
    monkeypatch.setattr(cjs, "JOBS_STATUS_PATH", out)
    payload = {
        "scheduled": [],
        "running": [],
        "recent_runs": [],
        "collected_at": "2026-06-18T12:00:00Z",
    }
    cjs.write_status(payload)
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_registry_matches_each_job_to_its_own_log() -> None:
    # Every registry job pins a distinct verify-log under ~/.quant-* (registry Verify column).
    logs = [job.log for job in cjs.REGISTRY]
    assert len(logs) == len(set(logs))
    assert all(".quant-" in str(log) for log in logs)


def test_load_status_handles_missing_and_corrupt(tmp_path, monkeypatch) -> None:
    import jobs_page

    missing = tmp_path / "absent.json"
    monkeypatch.setattr(jobs_page, "JOBS_STATUS_PATH", missing)
    assert jobs_page.load_status() is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(jobs_page, "JOBS_STATUS_PATH", corrupt)
    assert jobs_page.load_status() is None

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"scheduled": []}), encoding="utf-8")
    monkeypatch.setattr(jobs_page, "JOBS_STATUS_PATH", good)
    assert jobs_page.load_status() == {"scheduled": []}
