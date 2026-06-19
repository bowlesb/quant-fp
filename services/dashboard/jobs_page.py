"""Server-side render of the ``/jobs`` visibility page.

Three read-only sections fed by ``~/.quant-ops/jobs_status.json`` (written on the host by
``ops/collect_jobs_status.py``, mounted into the container at ``/quant-ops``):

  - **Scheduled** — the production shell crons (name | schedule | last run | status badge | purpose).
  - **Currently Running** — ad-hoc docker job containers (backfills / sweeps / dev runs).
  - **Recent Runs** — the most recent run event per job, newest first.

Pure server-rendered HTML (mirrors :mod:`status_page`), auto-refreshing every 60s. The page never writes;
it only reflects whatever the collector last wrote. A missing/empty status file renders a "not collected
yet" notice rather than erroring.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
from typing import Any


# Same mount as the status store; the collector writes jobs_status.json beside status_dashboard.json. An
# explicit JOBS_STATUS_PATH override wins, else derive from STATUS_STORE_PATH's directory, else the default.
def _default_jobs_path() -> Path:
    explicit = os.environ.get("JOBS_STATUS_PATH")
    if explicit:
        return Path(explicit)
    status_path = os.environ.get("STATUS_STORE_PATH")
    if status_path:
        return Path(status_path).parent / "jobs_status.json"
    return Path("/quant-ops/jobs_status.json")


JOBS_STATUS_PATH = _default_jobs_path()

# status -> (label, css class). Unknown statuses fall back to the neutral badge.
STATUS_BADGES = {
    "ok": ("ok", "badge-ok"),
    "failed": ("failed", "badge-bad"),
    "stale": ("stale", "badge-warn"),
    "unknown": ("unknown", "badge-muted"),
}

JOBS_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#d7dce2; }
  header { background:#171a21; padding:16px 24px; border-bottom:1px solid #262b35; }
  h1 { font-size:18px; margin:0; }
  header a { color:#58a6ff; text-decoration:none; font-size:13px; }
  .wrap { padding:20px 24px; max-width:1100px; }
  .muted { color:#8b949e; font-size:12px; }
  h2 { font-size:15px; margin:24px 0 10px; border-bottom:1px solid #262b35; padding-bottom:6px; }
  table.jobs { border-collapse:collapse; width:100%; font-size:12.5px; margin-bottom:8px; }
  table.jobs th, table.jobs td {
    text-align:left; padding:8px 10px; border:1px solid #262b35; vertical-align:top;
  }
  table.jobs thead th { background:#171a21; font-size:12px; }
  td.nowrap, th.nowrap { white-space:nowrap; font-variant-numeric:tabular-nums; }
  code { background:#0f1115; padding:1px 5px; border-radius:4px; font-size:12px; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge-ok { background:#23863633; color:#3fb950; }
  .badge-bad { background:#da363322; color:#f85149; }
  .badge-warn { background:#9e6a0322; color:#d29922; }
  .badge-muted { background:#30363d44; color:#8b949e; }
  .empty { color:#4b525e; }
</style>
"""


def render_badge(status: str) -> str:
    label, css = STATUS_BADGES.get(status, STATUS_BADGES["unknown"])
    return f"<span class='badge {css}'>{html.escape(label)}</span>"


def load_status() -> dict[str, Any] | None:
    """Read jobs_status.json; None if missing/empty/corrupt so the page can show a friendly notice."""
    if not JOBS_STATUS_PATH.exists():
        return None
    text = JOBS_STATUS_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data


def render_scheduled(scheduled: list[dict[str, Any]]) -> str:
    if not scheduled:
        return "<p class='muted empty'>No scheduled jobs in the registry.</p>"
    rows = ""
    for job in scheduled:
        last_run_value = job.get("last_run")
        last_run_html = (
            html.escape(str(last_run_value))
            if last_run_value
            else "<span class='empty'>never</span>"
        )
        log = job.get("log") or ""
        rows += (
            "<tr>"
            f"<td class='nowrap'>{html.escape(str(job.get('name', '')))}</td>"
            f"<td class='nowrap'>{html.escape(str(job.get('schedule', '')))}</td>"
            f"<td class='nowrap'>{last_run_html}</td>"
            f"<td>{render_badge(str(job.get('status', 'unknown')))}</td>"
            f"<td>{html.escape(str(job.get('purpose', '')))}<br><code>{html.escape(log)}</code></td>"
            "</tr>"
        )
    return (
        "<table class='jobs'><thead><tr>"
        "<th>name</th><th>schedule</th><th class='nowrap'>last run (UTC)</th>"
        "<th>status</th><th>purpose / verify-log</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def render_running(running: list[dict[str, Any]]) -> str:
    if not running:
        return "<p class='muted empty'>No ad-hoc job containers running.</p>"
    rows = "".join(
        "<tr>"
        f"<td class='nowrap'>{html.escape(str(job.get('name', '')))}</td>"
        f"<td>{html.escape(str(job.get('status', '')))}</td>"
        "</tr>"
        for job in running
    )
    return (
        "<table class='jobs'><thead><tr><th>container</th><th>status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_recent(recent_runs: list[dict[str, Any]]) -> str:
    if not recent_runs:
        return "<p class='muted empty'>No recent runs recorded.</p>"
    rows = "".join(
        "<tr>"
        f"<td class='nowrap'>{html.escape(str(run.get('ts', '')))}</td>"
        f"<td class='nowrap'>{html.escape(str(run.get('job', '')))}</td>"
        f"<td>{render_badge(str(run.get('status', 'unknown')))}</td>"
        "</tr>"
        for run in recent_runs
    )
    return (
        "<table class='jobs'><thead><tr><th class='nowrap'>when (UTC)</th><th>job</th><th>status</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def _collected_note(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    collected = data.get("collected_at")
    if not collected:
        return ""
    return f"collected {html.escape(str(collected))}"


def render_jobs_page(data: dict[str, Any] | None) -> str:
    if data is None:
        sections = (
            "<p class='muted'>No jobs status collected yet. The host collector "
            "(<code>ops/collect_jobs_status.py</code>) writes "
            "<code>~/.quant-ops/jobs_status.json</code> every ~5 min.</p>"
        )
        note = ""
    else:
        scheduled = data.get("scheduled", [])
        running = data.get("running", [])
        recent = data.get("recent_runs", [])
        sections = (
            f"<h2>Scheduled crons</h2>{render_scheduled(scheduled)}"
            f"<h2>Currently running</h2>{render_running(running)}"
            f"<h2>Recent runs</h2>{render_recent(recent)}"
        )
        note = _collected_note(data)

    rendered_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Jobs — Quant</title>
<meta http-equiv="refresh" content="60">
{JOBS_STYLE}</head>
<body>
<header><h1>Scheduled Jobs &nbsp;
<a href="/">&larr; dashboard</a> &nbsp;
<a href="/status">hourly status &rarr;</a> &nbsp;
<a href="/feature-grid">feature coverage &amp; trust &rarr;</a></h1>
<div class="muted">crons, running job containers &amp; recent runs &middot; auto-refreshes every 60s &middot; {html.escape(note)} &middot; page rendered {html.escape(rendered_at)}Z</div></header>
<div class="wrap">{sections}</div>
</body></html>"""
