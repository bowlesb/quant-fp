"""Server-side render of the ``/status`` hourly status dashboard.

One TABLE: each ROW is an hourly snapshot (newest on top), each COLUMN a workstream. Every cell shows
**Progress** and (only when present) **Blockers**. A per-row reaction box lets Ben type a reaction that
POSTs to ``/api/status/reaction`` and persists via :mod:`status_store`. Pure HTML + a sprinkle of vanilla
JS for the reaction POST — no framework, fast to load. The page reads the store live on each request, so a
new Lead-appended row appears on the next browser refresh.
"""
from __future__ import annotations

import html
from typing import Any

from status_store import WORKSTREAMS

STATUS_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#d7dce2; }
  header { background:#171a21; padding:16px 24px; border-bottom:1px solid #262b35; }
  h1 { font-size:18px; margin:0; }
  header a { color:#58a6ff; text-decoration:none; font-size:13px; }
  .wrap { padding:20px 24px; }
  .muted { color:#8b949e; font-size:12px; }
  table.status { border-collapse:collapse; width:100%; font-size:12.5px; }
  table.status th, table.status td {
    text-align:left; padding:8px 10px; border:1px solid #262b35; vertical-align:top;
  }
  table.status thead th { background:#171a21; position:sticky; top:0; font-size:12px; }
  th.ts-col, td.ts-col { white-space:nowrap; font-variant-numeric:tabular-nums; min-width:120px; }
  .cell-progress { color:#d7dce2; line-height:1.4; }
  .cell-blockers { margin-top:6px; color:#f0883e; line-height:1.4; }
  .cell-blockers::before { content:"⚠ "; }
  .cell-empty { color:#4b525e; }
  .reaction-cell { min-width:200px; background:#13161c; }
  .reaction-box { width:100%; box-sizing:border-box; background:#0f1115; color:#d7dce2;
    border:1px solid #30363d; border-radius:6px; padding:6px 8px; font-size:12.5px;
    font-family:inherit; resize:vertical; min-height:46px; }
  .reaction-box:focus { outline:none; border-color:#1f6feb; }
  .reaction-row { margin-top:6px; display:flex; align-items:center; gap:8px; }
  .reaction-save { background:#1f6feb; color:#fff; border:none; border-radius:6px; padding:4px 12px;
    font-size:12px; cursor:pointer; }
  .reaction-save:hover { background:#388bfd; }
  .reaction-status { font-size:11px; color:#3fb950; }
</style>
"""

STATUS_SCRIPT = """
<script>
async function saveReaction(ts, btn) {
  const cell = btn.closest('.reaction-cell');
  const box = cell.querySelector('.reaction-box');
  const note = cell.querySelector('.reaction-status');
  btn.disabled = true;
  note.textContent = 'saving…';
  const resp = await fetch('/api/status/reaction', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ts: ts, text: box.value})
  });
  btn.disabled = false;
  note.textContent = resp.ok ? 'saved ✓' : 'error';
  if (resp.ok) { setTimeout(() => { note.textContent = ''; }, 2500); }
}
</script>
"""


def render_cell(cell: dict[str, str] | None) -> str:
    """One workstream cell: Progress always shown (em-dash if empty), Blockers only when non-empty."""
    if not cell:
        return "<span class='cell-empty'>—</span>"
    progress = cell.get("progress", "").strip()
    blockers = cell.get("blockers", "").strip()
    progress_html = (
        f"<div class='cell-progress'>{html.escape(progress)}</div>"
        if progress
        else "<div class='cell-progress cell-empty'>—</div>"
    )
    blockers_html = (
        f"<div class='cell-blockers'>{html.escape(blockers)}</div>" if blockers else ""
    )
    return progress_html + blockers_html


def render_reaction_cell(ts: str, reaction: str) -> str:
    ts_attr = html.escape(ts, quote=True)
    return (
        "<td class='reaction-cell'>"
        f"<textarea class='reaction-box' placeholder='Ben: type a reaction…'>{html.escape(reaction)}</textarea>"
        "<div class='reaction-row'>"
        f"<button class='reaction-save' onclick=\"saveReaction('{ts_attr}', this)\">Save</button>"
        "<span class='reaction-status'></span>"
        "</div></td>"
    )


def render_status_page(rows: list[dict[str, Any]]) -> str:
    header_cells = "".join(f"<th>{html.escape(name)}</th>" for name in WORKSTREAMS)
    thead = (
        "<thead><tr><th class='ts-col'>Snapshot (UTC)</th>"
        f"{header_cells}<th>Ben reaction</th></tr></thead>"
    )

    if rows:
        body_rows = ""
        for row in rows:
            cells = row.get("cells", {})
            cell_html = "".join(f"<td>{render_cell(cells.get(name))}</td>" for name in WORKSTREAMS)
            body_rows += (
                f"<tr><td class='ts-col'>{html.escape(row['ts'])}</td>"
                f"{cell_html}"
                f"{render_reaction_cell(row['ts'], row.get('reaction', ''))}</tr>"
            )
        table_body = f"<tbody>{body_rows}</tbody>"
    else:
        span = len(WORKSTREAMS) + 2
        table_body = (
            f"<tbody><tr><td colspan='{span}' class='muted'>No status snapshots yet. "
            "The Lead loop appends one per cycle.</td></tr></tbody>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Status — Quant</title>
<meta http-equiv="refresh" content="120">
{STATUS_STYLE}</head>
<body>
<header><h1>Hourly Status Dashboard &nbsp;
<a href="/">&larr; dashboard</a> &nbsp;
<a href="/feature-grid">feature coverage &amp; trust &rarr;</a></h1>
<div class="muted">newest snapshot on top &middot; auto-refreshes every 2 min &middot; per-row reaction persists for the Lead to review</div></header>
<div class="wrap">
<table class="status">{thead}{table_body}</table>
</div>
{STATUS_SCRIPT}
</body></html>"""
