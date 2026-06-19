"""Local-network dashboard for the quant trading system.

Serves a single status page on the LAN showing build progress (rendered from
STATE.md / JOURNAL.md) and live system health queried directly from TimescaleDB.
No secrets; the only write is Ben's per-row reaction on the /status board (a free-text
note persisted to the append-only status store). Ben checks it in a browser; Claude
reads /status.json or the DB directly in-session.
"""
import html
import os
import re
from pathlib import Path
from typing import Any

import markdown
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import status_store
from feature_grid import CACHE, STORE_ROOT
from feature_grid_page import FEATURE_GRID_HTML
from jobs_page import load_status as load_jobs_status
from jobs_page import render_jobs_page
from liquidity_bands import CACHE as BANDS_CACHE
from liquidity_bands_page import LIQUIDITY_BANDS_HTML
from raw_coverage import CACHE as RAW_CACHE
from raw_coverage_page import RAW_COVERAGE_HTML
from status_page import render_status_page

app = FastAPI(title="Quant Dashboard")

DOCS_DIR = Path("/docs")
# 8-hour progress reports, one markdown file per period (market/evening/overnight).
# Mounted read-only; the page renders whatever .md files appear here, no rebuild
# needed when the Manager drops a new report.
PROGRESS_DIR = DOCS_DIR / "progress"

# Roadmap/severity tags the Manager uses in reports, e.g. [M2] or [P3]. Highlighted
# visually so a skim surfaces which milestone each line ladders up to.
TAG_PATTERN = re.compile(r"\[((?:M|P)\d[\w/]*)\]")

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

# Tables whose row counts and latest-timestamp we surface as health signals.
TRACKED_TABLES = [
    "bars_1m",
    "quote_agg_1m",
    "trade_agg_1m",
    "trades_raw",
    "news",
    "feature_vectors",
    "predictions",
    "orders_log",
    "fills_log",
]
TABLES_WITH_TS = {
    "bars_1m",
    "quote_agg_1m",
    "trade_agg_1m",
    "trades_raw",
    "news",
    "feature_vectors",
    "predictions",
}


def collect_metrics() -> dict[str, Any]:
    """Query live system health from the database."""
    metrics: dict[str, Any] = {"db_ok": False, "tables": {}}
    try:
        with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn:
            metrics["db_ok"] = True
            with conn.cursor() as cur:
                for table in TRACKED_TABLES:
                    row: dict[str, Any] = {}
                    cur.execute(f"SELECT count(*) FROM {table}")
                    count_row = cur.fetchone()
                    row["rows"] = count_row[0] if count_row else 0
                    if table in TABLES_WITH_TS:
                        ts_col = "created_at" if table == "news" else "ts"
                        cur.execute(f"SELECT max({ts_col}) FROM {table}")
                        ts_row = cur.fetchone()
                        row["latest"] = ts_row[0].isoformat() if ts_row and ts_row[0] else None
                    metrics["tables"][table] = row

                cur.execute(
                    "SELECT ok, ts FROM reconciliation_log ORDER BY ts DESC LIMIT 1"
                )
                recon = cur.fetchone()
                metrics["last_reconciliation"] = (
                    {"ok": recon[0], "ts": recon[1].isoformat()} if recon else None
                )

                cur.execute(
                    """
                    SELECT trade_date, symbol, received_minutes, expected_minutes
                    FROM data_quality_daily
                    WHERE trade_date = (SELECT max(trade_date) FROM data_quality_daily)
                    ORDER BY symbol
                    """
                )
                metrics["coverage"] = [
                    {
                        "date": r[0].isoformat(),
                        "symbol": r[1],
                        "received": r[2],
                        "expected": r[3],
                        "pct": round(100.0 * r[2] / r[3], 1) if r[3] else 0.0,
                    }
                    for r in cur.fetchall()
                ]
    except psycopg.Error as exc:
        metrics["error"] = str(exc)
    return metrics


def render_doc(name: str) -> str:
    path = DOCS_DIR / name
    if not path.exists():
        return f"<p><em>{name} not mounted.</em></p>"
    return markdown.markdown(
        path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )


def highlight_tags(rendered_html: str) -> str:
    """Wrap roadmap/severity tags like [M2] / [P3] in a styled chip so they stand
    out in a skim. Runs on already-escaped rendered HTML, so it only ever inserts
    our own span markup around the literal bracketed tag text."""
    return TAG_PATTERN.sub(r'<span class="tag">[\1]</span>', rendered_html)


def list_progress_reports() -> list[str]:
    """Report filenames (e.g. 2026-06-12_market.md), newest-first. The date_period
    naming sorts chronologically as a string, so reverse-sort is newest-first; ties
    within a day fall back to file mtime so a same-day re-drop still orders right."""
    if not PROGRESS_DIR.exists():
        return []
    files = [p for p in PROGRESS_DIR.glob("*.md") if p.is_file()]
    files.sort(key=lambda p: (p.stem, p.stat().st_mtime), reverse=True)
    return [p.name for p in files]


def render_progress_report(name: str) -> str:
    """Render one progress report's markdown with roadmap tags highlighted. `name`
    is validated against the directory listing by the caller, so no path traversal."""
    path = PROGRESS_DIR / name
    rendered = markdown.markdown(
        path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )
    return highlight_tags(rendered)


@app.get("/status.json")
def status_json() -> JSONResponse:
    return JSONResponse(collect_metrics())


@app.get("/api/feature-grid")
def feature_grid_json(refresh: bool = False) -> JSONResponse:
    """The full coverage + trust grid as JSON — the SAME data the /feature-grid UI renders.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {generated_at, store_root, anchor_date, earliest_date,
       periods: [{key, label, lookback_days}],
       groups:  [{group, version, layer, n_features}],
       cells:   [{group, period, coverage_pct, stream_pct, backfill_pct, n_features,
                  n_symbols, n_dates, trust_state, trust_pct, n_trusted, n_validating, n_ungraded}],
       summary: {n_groups, n_features, n_trusted, trusted_pct, mean_coverage_pct,
                 fully_validated_groups, days_needed_for_trust}}
    ``refresh=1`` bypasses the TTL cache and re-aggregates.
    """
    return JSONResponse(CACHE.grid(STORE_ROOT, force=refresh))


@app.get("/api/feature-grid/thin-live-symbols")
def feature_grid_thin_live_json(limit: int = 50, refresh: bool = False) -> JSONResponse:
    """Cross-group THINNEST-live ticker roll-up: which SYMBOLS are present in the full-universe backfill agg
    but absent from the live stream across the MOST groups — the system-wide ticker-representation flag for the
    FP_TICK_SYMBOLS coverage gap (the inverse of the per-group ``/symbols`` surface). Under-representation is
    scored only over LIVE groups (non-empty stream universe today).

    Registered BEFORE ``/{group}`` so the static path is not swallowed by the path param. ``limit`` caps the
    ranked symbol list; ``refresh=1`` bypasses the TTL cache.

    Shape: {generated_at, store_root, n_live_groups, n_groups, n_thin_symbols, limit,
            symbols: [{symbol, n_under_groups, n_live_groups, under_groups: [...]}],
            groups:  [{group, live, n_stream, n_backfill, n_under}]}
    """
    return JSONResponse(CACHE.thin_live(STORE_ROOT, limit=limit, force=refresh))


@app.get("/api/feature-grid/timeline")
def feature_grid_timeline_json(days: int = 21, refresh: bool = False) -> JSONResponse:
    """The (group x recent-day x source) PRESENCE grid + per-group DEPTH stats — the time/depth legibility
    view. For the last ``days`` calendar days (most-recent first, ending at the latest store date), each
    (group, day) cell carries the stream/backfill symbol counts and a provenance class (both / stream_only /
    backfill_only / absent), so live-vs-backfill provenance per (group, day) reads off the grid. Each group
    also carries history depth (``backfill_earliest`` + ``backfill_span_days``) and live horizon
    (``stream_horizon_days``: recent weekdays the stream captured unbroken).

    Registered BEFORE ``/{group}`` so the static path is not swallowed by the path param. ``refresh=1``
    bypasses the TTL cache.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {generated_at, store_root, anchor_date, earliest_date, days, dates: [...],
       groups: [{group, version, layer, n_features, backfill_earliest, backfill_latest, backfill_span_days,
                 stream_earliest, stream_latest, stream_horizon_days,
                 days: [{date, stream, backfill, provenance}]}]}
    """
    return JSONResponse(CACHE.timeline(STORE_ROOT, days=days, force=refresh))


@app.get("/api/feature-grid/orderflow-trend")
def feature_grid_orderflow_trend_json(days: int = 21, refresh: bool = False) -> JSONResponse:
    """Per-recent-day LIVE-stream breadth across the order-flow groups — is FP_TICK_SYMBOLS coverage WIDENING
    or STALLING at the ~24-canary floor? For the last ``days`` calendar days (most-recent first, ending at the
    latest order-flow stream date), each day carries the DISTINCT symbol count live in at least one order-flow
    group (``n_union``, the headline trend), the count live in EVERY capturing order-flow group
    (``n_intersection``, the full-coverage core), the per-group stream counts, and the window's first-vs-last
    captured ``union_delta`` (>0 widening, 0 flat, <0 shrinking). This is the symbol-level trend the per-group
    timeline and the latest-day per-symbol surfaces don't give — the certification readiness signal for live
    order-flow.

    Registered BEFORE ``/{group}`` so the static path is not swallowed by the path param. ``refresh=1``
    bypasses the TTL cache.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {generated_at, store_root, anchor_date, days, groups: [...], dates: [...],
       newest_captured_union, oldest_captured_union, union_delta,
       trend: [{date, n_union, n_intersection, n_live_groups, per_group: {group: n}}]}
    """
    return JSONResponse(CACHE.orderflow_trend(STORE_ROOT, days=days, force=refresh))


@app.get("/api/feature-grid/{group}")
def feature_grid_group_json(group: str, refresh: bool = False) -> JSONResponse:
    """Per-feature detail for one group (the expanded view) as JSON.

    Shape: {group, version, n_features, stream_dates, backfill_dates, stream_first/last,
            backfill_first/last, stream_only_dates, backfill_only_dates,
            features: [{feature, description, layer, parity_method, trust_state, clean_days,
                        days_needed, progress_to_trusted_pct, clean_value_rate, last_validated_day}]}
    """
    try:
        return JSONResponse(CACHE.detail(group, STORE_ROOT, force=refresh))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown group '{group}'") from exc


@app.get("/api/feature-grid/{group}/symbols")
def feature_grid_symbols_json(group: str, refresh: bool = False) -> JSONResponse:
    """Per-SYMBOL coverage for one group on its latest store date: which tickers the live STREAM captured vs
    which exist only in BACKFILL — the ticker-representation surface (``backfill_only`` = under-represented
    LIVE). Same data the group-detail UI's symbol drill-in renders.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {group, version, stream_date, backfill_date, n_stream, n_backfill, n_both, n_backfill_only,
       n_stream_only, stream_coverage_pct, both: [...], backfill_only: [...], stream_only: [...]}
    """
    try:
        return JSONResponse(CACHE.symbols(group, STORE_ROOT, force=refresh))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown group '{group}'") from exc


@app.get("/feature-grid", response_class=HTMLResponse)
def feature_grid_page() -> str:
    """The visual coverage + trust grid (vanilla HTML/JS; fetches /api/feature-grid client-side)."""
    return FEATURE_GRID_HTML


@app.get("/api/raw-coverage")
def raw_coverage_json(days: int = 90, refresh: bool = False) -> JSONResponse:
    """RAW-TAPE coverage: what raw Alpaca history exists per layer (minute bars / tick trades / tick quotes) —
    the substrate modellers invent features on, read straight from the raw manifests (cheap, NO store scan).

    Per layer: DEPTH (earliest/latest date + span days) and BREADTH (distinct symbols-per-day over time, so
    the thin-trades / shallow-quotes gaps read off at a glance), plus a per-date timeline (which dates present,
    gaps visible). All computed over REAL cells (rows>0); 0-row settled-empty manifest markers are excluded.
    ``days`` clips each layer's per-date timeline to the most-recent N calendar days (``days=0`` = full
    history); summary depth/breadth stats are always over the full tape. ``refresh=1`` bypasses the TTL cache.

    Shape (see docs/RAW_TAPE_COVERAGE.md):
      {generated_at, store_root, days, anchor_date, span_earliest, span_latest,
       layers: [{tier, label, earliest, latest, span_days, n_dates, n_symbols, n_cells,
                 mean_symbols_per_day, median_symbols_per_day, newest_symbols_per_day,
                 shown_from, n_dates_shown, dates: [{date, n_symbols, rows}]}]}
    """
    return JSONResponse(RAW_CACHE.coverage(STORE_ROOT, days=days, force=refresh))


@app.get("/raw-coverage", response_class=HTMLResponse)
def raw_coverage_page() -> str:
    """The visual raw-tape coverage surface (vanilla HTML/JS; fetches /api/raw-coverage client-side)."""
    return RAW_COVERAGE_HTML


@app.get("/api/liquidity-bands")
def liquidity_bands_json(window_days: int = 85, refresh: bool = False) -> JSONResponse:
    """The canonical ADV-rank / liquidity-band reference surface — ONE liquidity partition the research lanes
    can reference instead of each re-deriving its own (Lane C bands, FeatureInventor top-400, pilot top-500).

    Reduces the most-recent ``window_days`` raw-bar dates to per-symbol RTH dollar volume, computes trailing-
    20d ADV + each symbol's stable cross-sectional rank, assigns the canonical bands (B1..B5, lo inclusive /
    hi exclusive by ADV rank — the same cut the overnight boundary adjudication ran on), and returns per-band
    COMPOSITION (sizes + ADV ranges on the anchor date) + membership STABILITY (point-in-time band turnover
    over the window). Read-side only (raw bars, store mounted read-only); ~25-30s cold then a 10-min TTL.
    ``refresh=1`` bypasses the cache.

    Shape (see docs/LIQUIDITY_BANDS.md):
      {generated_at, store_root, window_days, anchor_date, window_first, window_last, n_dates,
       n_ranked_symbols, adv_window, min_days_for_rank,
       bands: [{band, label, rank_lo, rank_hi, n_symbols, adv_min, adv_median, adv_max}],
       stability: [{band, n_today, retained_5d_pct, retained_20d_pct}]}
    """
    return JSONResponse(BANDS_CACHE.surface(STORE_ROOT, window_days=window_days, force=refresh))


@app.get("/api/liquidity-bands/symbol/{symbol}")
def liquidity_bands_symbol_json(symbol: str, window_days: int = 85) -> JSONResponse:
    """One symbol's current liquidity placement: stable ADV, cross-sectional rank, band, latest trailing-20d
    ADV. ``found=false`` when the symbol is below the rank floor / absent from the window."""
    return JSONResponse(BANDS_CACHE.lookup(symbol, STORE_ROOT, window_days=window_days))


@app.get("/api/liquidity-bands/members/{band}")
def liquidity_bands_members_json(band: str, window_days: int = 85, limit: int = 250) -> JSONResponse:
    """The symbols in one band on the anchor date, ordered by ADV rank (most liquid first), capped at
    ``limit`` — a band's universe a lane can pull directly instead of re-deriving the cut."""
    return JSONResponse(BANDS_CACHE.members(band, STORE_ROOT, window_days=window_days, limit=limit))


@app.get("/liquidity-bands", response_class=HTMLResponse)
def liquidity_bands_page() -> str:
    """The visual liquidity-band surface (vanilla HTML/JS; fetches /api/liquidity-bands client-side)."""
    return LIQUIDITY_BANDS_HTML


class ReactionRequest(BaseModel):
    ts: str
    text: str


@app.get("/api/status/rows")
def status_rows_json() -> JSONResponse:
    """The hourly status snapshots NEWEST-FIRST — the same data the /status page renders.

    Shape: [{ts, cells: {workstream: {progress, blockers}}, reaction}, ...] (see status_store).
    """
    return JSONResponse(status_store.read_rows())


@app.post("/api/status/reaction")
def status_set_reaction(req: ReactionRequest) -> JSONResponse:
    """Persist Ben's reaction text onto the snapshot row identified by ``ts`` (last write wins)."""
    if not status_store.set_reaction(req.ts, req.text):
        raise HTTPException(status_code=404, detail=f"no status row with ts '{req.ts}'")
    return JSONResponse({"ok": True, "ts": req.ts})


@app.get("/status", response_class=HTMLResponse)
def status_page() -> str:
    """The hourly status board: one row per snapshot, columns per workstream, per-row Ben-reaction box."""
    return render_status_page(status_store.read_rows())


@app.get("/api/jobs")
def jobs_json() -> JSONResponse:
    """The jobs status the /jobs page renders — scheduled crons, running job containers, recent runs.

    Shape (see ops/collect_jobs_status.py): {scheduled, running, recent_runs, collected_at}. Returns an
    empty-but-valid shape if the host collector has not written jobs_status.json yet.
    """
    data = load_jobs_status()
    if data is None:
        data = {"scheduled": [], "running": [], "recent_runs": [], "collected_at": None}
    return JSONResponse(data)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page() -> str:
    """Read-only jobs visibility: scheduled crons + last-run status, running job containers, recent runs."""
    return render_jobs_page(load_jobs_status())


PROGRESS_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#d7dce2; }
  header { background:#171a21; padding:16px 24px; border-bottom:1px solid #262b35; }
  h1 { font-size:18px; margin:0; }
  header a { color:#58a6ff; text-decoration:none; font-size:13px; }
  .layout { display:grid; grid-template-columns:240px 1fr; gap:0; min-height:calc(100vh - 58px); }
  .sidebar { background:#13161c; border-right:1px solid #262b35; padding:16px; }
  .sidebar a { display:block; color:#d7dce2; text-decoration:none; padding:8px 10px;
               border-radius:6px; font-size:13px; margin-bottom:2px; }
  .sidebar a:hover { background:#1d212a; }
  .sidebar a.active { background:#1f6feb33; color:#58a6ff; font-weight:600; }
  .content { padding:24px 32px; max-width:900px; }
  .doc { font-size:14px; line-height:1.55; }
  .doc h1 { font-size:20px; border-bottom:1px solid #262b35; padding-bottom:8px; }
  .doc h2 { font-size:16px; border-bottom:1px solid #262b35; padding-bottom:4px; margin-top:24px; }
  .doc code { background:#0f1115; padding:1px 4px; border-radius:3px; }
  .doc pre { background:#0f1115; padding:12px; border-radius:6px; overflow:auto; }
  .doc table { border-collapse:collapse; font-size:13px; }
  .doc th,.doc td { text-align:left; padding:6px 8px; border-bottom:1px solid #262b35; }
  .tag { background:#1f6feb33; color:#58a6ff; padding:0 5px; border-radius:4px;
         font-size:0.85em; font-weight:600; font-variant-numeric:tabular-nums; }
  .muted { color:#8b949e; font-size:12px; }
</style>
"""


@app.get("/progress", response_class=HTMLResponse)
def progress(report: str | None = None) -> str:
    reports = list_progress_reports()
    if not reports:
        body = "<div class='content'><p class='muted'>No progress reports yet.</p></div>"
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Progress — Quant</title>{PROGRESS_STYLE}</head><body>
<header><h1>Progress Reports &nbsp; <a href="/">&larr; dashboard</a></h1></header>
{body}</body></html>"""

    selected = report if report in reports else reports[0]
    links = "".join(
        f'<a class="{"active" if name == selected else ""}" '
        f'href="/progress?report={html.escape(name, quote=True)}">'
        f'{html.escape(name.removesuffix(".md"))}</a>'
        for name in reports
    )
    doc_html = render_progress_report(selected)

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Progress — {html.escape(selected)}</title>{PROGRESS_STYLE}</head><body>
<header><h1>Progress Reports &nbsp; <a href="/">&larr; dashboard</a></h1>
<div class="muted">8-hour cadence: market / evening / overnight &middot; newest first</div></header>
<div class="layout">
  <nav class="sidebar">{links}</nav>
  <div class="content doc">{doc_html}</div>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    metrics = collect_metrics()
    db_badge = (
        "<span class='ok'>● connected</span>"
        if metrics["db_ok"]
        else "<span class='bad'>● down</span>"
    )

    rows_html = ""
    for table, info in metrics.get("tables", {}).items():
        latest = info.get("latest") or "—"
        rows_html += (
            f"<tr><td>{table}</td><td class='num'>{info['rows']:,}</td>"
            f"<td>{latest}</td></tr>"
        )

    recon = metrics.get("last_reconciliation")
    recon_html = "no reconciliation yet" if not recon else (
        f"{'OK' if recon['ok'] else 'MISMATCH'} @ {recon['ts']}"
    )

    coverage = metrics.get("coverage", [])
    if coverage:
        cov_rows = "".join(
            f"<tr><td>{c['symbol']}</td><td class='num'>{c['received']}/{c['expected']}</td>"
            f"<td class='num'>{c['pct']}%</td></tr>"
            for c in coverage
        )
        cov_html = (
            f"<h2 style='margin-top:0;font-size:15px;'>Coverage ({coverage[0]['date']})</h2>"
            f"<table><thead><tr><th>symbol</th><th class='num'>bars</th>"
            f"<th class='num'>coverage</th></tr></thead><tbody>{cov_rows}</tbody></table>"
        )
    else:
        cov_html = "<h2 style='margin-top:0;font-size:15px;'>Coverage</h2><p class='muted'>no coverage data yet</p>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quant Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#d7dce2; }}
  header {{ background:#171a21; padding:16px 24px; border-bottom:1px solid #262b35; }}
  h1 {{ font-size:18px; margin:0; }}
  .wrap {{ padding:24px; max-width:1100px; margin:0 auto; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  .card {{ background:#171a21; border:1px solid #262b35; border-radius:8px; padding:16px 20px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #262b35; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .ok {{ color:#3fb950; }} .bad {{ color:#f85149; }}
  .doc {{ font-size:14px; line-height:1.5; }}
  .doc h2 {{ font-size:15px; border-bottom:1px solid #262b35; padding-bottom:4px; }}
  .doc code {{ background:#0f1115; padding:1px 4px; border-radius:3px; }}
  .doc pre {{ background:#0f1115; padding:12px; border-radius:6px; overflow:auto; }}
  .muted {{ color:#8b949e; font-size:12px; }}
</style></head>
<body>
<header><h1>Quant Trading System &nbsp; {db_badge} &nbsp;
<a href="/status" style="color:#58a6ff;text-decoration:none;font-size:13px;">Hourly status &rarr;</a> &nbsp;
<a href="/jobs" style="color:#58a6ff;text-decoration:none;font-size:13px;">Jobs &rarr;</a> &nbsp;
<a href="/progress" style="color:#58a6ff;text-decoration:none;font-size:13px;">Progress reports &rarr;</a> &nbsp;
<a href="/feature-grid" style="color:#58a6ff;text-decoration:none;font-size:13px;">Feature coverage &amp; trust &rarr;</a> &nbsp;
<a href="/raw-coverage" style="color:#58a6ff;text-decoration:none;font-size:13px;">Raw-tape coverage &rarr;</a> &nbsp;
<a href="/liquidity-bands" style="color:#58a6ff;text-decoration:none;font-size:13px;">Liquidity bands &rarr;</a></h1>
<div class="muted">auto-refreshes every 30s &middot; reconciliation: {recon_html}</div></header>
<div class="wrap">
  <div class="grid" style="margin-bottom:24px;">
    <div class="card">
      <h2 style="margin-top:0;font-size:15px;">Data health</h2>
      <table><thead><tr><th>table</th><th class="num">rows</th><th>latest</th></tr></thead>
      <tbody>{rows_html}</tbody></table>
    </div>
    <div class="card">{cov_html}</div>
  </div>
  <div class="grid">
    <div class="card doc">{render_doc("STATE.md")}</div>
    <div class="card doc">{render_doc("JOURNAL.md")}</div>
  </div>
</div>
</body></html>"""
