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
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import scorecard_store
import status_store
from feature_grid import CACHE, STORE_ROOT
from jobs_page import load_status as load_jobs_status
from jobs_page import render_jobs_page
from liquidity_bands import CACHE as BANDS_CACHE
from liquidity_bands import band_members, parse_cuts, symbol_history
from liquidity_bands_page import LIQUIDITY_BANDS_HTML
from raw_coverage import CACHE as RAW_CACHE
from raw_coverage_page import RAW_COVERAGE_HTML
from scorecard import CACHE as SCORECARD_CACHE
from scorecard_page import SCORECARD_HTML
from sector_coverage import CACHE as SECTOR_CACHE
from sector_coverage_page import SECTOR_COVERAGE_HTML
from status_page import render_status_page
from store_glimpse import CACHE as GLIMPSE_CACHE
from store_glimpse_cache import read_drill as read_glimpse_drill
from store_glimpse_cache import read_glimpse
from store_grid_cache import read_drill as read_grid_drill
from store_grid_cache import read_grid_gzip, read_meta as read_grid_meta
from universe_coverage import CACHE as UNIVERSE_CACHE
from universe_coverage_page import UNIVERSE_COVERAGE_HTML

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

                cur.execute("SELECT ok, ts FROM reconciliation_log ORDER BY ts DESC LIMIT 1")
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
    return markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])


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
    rendered = markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
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


@app.get("/api/feature-grid/trust-frontier")
def feature_grid_trust_frontier_json(refresh: bool = False) -> JSONResponse:
    """The TRUST FRONTIER: how close the feature set is to fully trusted, split TRUSTED / ELIGIBLE / BLOCKED.

    The flat per-feature ``lifecycle_state`` badge cannot show that a DIVERGENT feature whose parity defect
    has been CLEARED is one clean sweep from TRUSTED (the state lags until the next sweep re-grades). This view
    joins ``feature_trust`` against the OPEN rows of ``feature_parity_defect`` (read-only) to make that frontier
    legible: ELIGIBLE = not-yet-trusted with NO open defect (advances on the next clean settled sweep), BLOCKED
    = still has an open parity defect (needs a fix; today the FP_TICK_SYMBOLS tick tail). ``projected_trusted_pct``
    = where trust lands if every eligible feature earns it on the next clean sweep — the headline of the jump.

    Registered BEFORE ``/{group}`` so the static path is not swallowed by the path param. ``refresh=1``
    bypasses the TTL cache.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {generated_at, n_features, n_trusted, n_eligible, n_blocked, n_open_defects,
       trusted_pct, eligible_pct, blocked_pct, projected_trusted_pct,
       groups: [{group, layer, n_features, n_trusted, n_eligible, n_blocked,
                 trusted_pct, projected_trusted_pct, blocked_features: [...]}]}
    """
    return JSONResponse(CACHE.frontier(force=refresh))


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


@app.get("/api/feature-grid/{group}/symbol-depth")
def feature_grid_symbol_depth_json(group: str, limit: int = 200, refresh: bool = False) -> JSONResponse:
    """Per-SYMBOL coverage DEPTH for one group: for each ticker, HOW FAR BACK its data goes (earliest →
    latest date + span + dates-present) PER SOURCE (stream vs backfill) — the time-DEPTH cut.

    ``/symbols`` is per-symbol but only on the LATEST date (no depth); ``/timeline`` is depth but at the GROUP
    level. This is the intersection — *which TICKER has this FEATURE, how far back, and from which source* — so
    a ticker that backfills to 2025-05 but only streams the last 4 days reads exactly that. Each symbol is
    classified ``both`` / ``backfill_only`` (settled history, under-represented LIVE) / ``stream_only`` (live,
    not yet parity-checkable). ``limit`` caps the per-symbol rows (ranked shallowest-backfill first); summary
    counts/spans are over ALL symbols. Two path segments, so the static path is not swallowed by ``/{group}``.

    Shape (see docs/FEATURE_DASHBOARD.md):
      {group, version, n_symbols, n_both, n_backfill_only, n_stream_only,
       stream_earliest, stream_latest, stream_n_dates, backfill_earliest, backfill_latest, backfill_n_dates,
       limit, n_shown, symbols: [{symbol, provenance, stream_earliest, stream_latest, stream_span_days,
       stream_n_dates, backfill_earliest, backfill_latest, backfill_span_days, backfill_n_dates}]}
    """
    try:
        return JSONResponse(CACHE.symbol_depth(group, STORE_ROOT, limit=limit, force=refresh))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown group '{group}'") from exc


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


@app.get("/api/sector-coverage")
def sector_coverage_json(refresh: bool = False) -> JSONResponse:
    """SECTOR coverage: how much of the LIVE universe carries an FMP GICS-aligned sector label — a read-only
    join of ``sector_map`` onto the latest ``universe_membership`` snapshot. Surfaces the honest partial
    coverage (the sector unblock classified ~73% of the universe; the unmapped tail is mostly ETFs/warrants/
    preferred bucketed as ``sector_is_unknown`` by design) and flags under-represented sectors.

    Per the live universe: per-sector symbol COUNT (the 11 GICS sectors, ranked), the CLASSIFIED-vs-UNKNOWN
    split (unknown = blank-sector row OR no ``sector_map`` row), the CLASSIFIED % headline, and a sample of
    unclassified tickers. Plus whole-table ``sector_map`` totals. ``refresh=1`` bypasses the TTL cache.

    Shape (see docs/SECTOR_COVERAGE.md):
      {generated_at, universe_date, universe_size, n_classified, n_unknown, classified_pct,
       n_blank_sector, n_no_row, n_distinct_sectors,
       sectors: [{sector, n_symbols, pct_of_universe}], unclassified_sample: [...],
       sector_map: {n_rows, n_classified, n_distinct_sectors}}
    """
    return JSONResponse(SECTOR_CACHE.coverage(force=refresh))


@app.get("/sector-coverage", response_class=HTMLResponse)
def sector_coverage_page() -> str:
    """The visual sector-coverage surface (vanilla HTML/JS; fetches /api/sector-coverage client-side)."""
    return SECTOR_COVERAGE_HTML


@app.get("/api/universe-coverage")
def universe_coverage_json(days: int = 30, refresh: bool = False) -> JSONResponse:
    """UNIVERSE coverage: the CAPTURED universe (``universe_membership`` in_universe count per session) vs the
    AVAILABLE filtered set (tradable primary-venue common stocks surviving the seed's exchange + ETF/fund
    screen over ``asset_metadata``) — the whole-universe captured-vs-available ratio over time. The complement
    of the #223 per-group DROP detector: a silent universe re-cap (e.g. the 06-16 relaunch's 3000-of-7.3k
    default cap) is an instantly-visible, permanent fixture, not a one-time catch.

    Per day: captured count, the SAME-snapshot available denominator, the captured/available RATIO + status
    band (full/thinned/capped), and uncaptured = available − captured (names left on the table). A day captured
    ABOVE the current available set (a pre-ETF-screen seed) is flagged, ratio clamped to 100%. ``days`` clips
    the per-day timeline to the most-recent N captured sessions. ``refresh=1`` bypasses the TTL cache.

    Shape (see docs/UNIVERSE_COVERAGE.md):
      {generated_at, available, status, ratio_thresholds: {ok, thin},
       latest: {date, captured, ratio, ratio_pct, uncaptured, status, over_available},
       timeline: [{date, captured, ratio, ratio_pct, uncaptured, status, over_available}]}
    """
    return JSONResponse(UNIVERSE_CACHE.coverage(days=days, force=refresh))


@app.get("/universe-coverage", response_class=HTMLResponse)
def universe_coverage_page() -> str:
    """The visual universe-coverage surface (vanilla HTML/JS; fetches /api/universe-coverage client-side)."""
    return UNIVERSE_COVERAGE_HTML


@app.get("/api/store-glimpse")
def store_glimpse_json(days: int = 30, refresh: bool = False) -> JSONResponse:
    """The LIVE feature-store GLIMPSE grid: DATE rows × FEATURE-GROUP columns, each cell carrying coverage
    fraction (the cell DARKNESS = n_symbols-that-date / captured-universe) + trust hue (green=trusted /
    amber=pending / red=divergent / grey=ungraded). Plus a per-date Total column and an expandable
    per-feature breakdown. Reuses the #221/#223 grid's gathered counts + the feature_trust read — no new
    heavy store I/O. ``days`` clips the row window (most-recent first).

    SERVED FROM the persistent (Redis) cache that ``ops/collect_store_glimpse.py`` precomputes on a cron — a
    sub-ms read, so the refresh is instant and always warm; the ~50s grid build runs only in that background
    worker, never on this request. A COLD cache (worker not run yet) or unreachable Redis returns a small
    ``warming`` payload (the page shows 'warming…') rather than hanging the request on the live build.
    ``refresh=1`` is the manual escape hatch: it forces a live in-process build (the old path) for when the
    worker is down and a fresh grid is needed now.

    Shape (see docs/STORE_GLIMPSE.md):
      {generated_at, store_root, anchor_date, days, universe_size,
       summary: {n_groups, n_features, n_dates, n_trusted, trusted_pct, trust_counts},
       groups: [{group, version, n_features, trust_hue, trust_counts, features: [...]}],
       dates: ["2026-06-20", ...],
       cells: {date: {group: {coverage, n_symbols, hue}, ..., "__total__": {...}}}}
    """
    if refresh:
        return JSONResponse(GLIMPSE_CACHE.glimpse(STORE_ROOT, days=days, force=True))
    return JSONResponse(read_glimpse(days=days))


@app.get("/api/store-glimpse/{group}/tickers")
def store_glimpse_drill_json(
    group: str, days: int = 30, limit: int = 500, refresh: bool = False
) -> JSONResponse:
    """The drill-down for one (date × group) cell: a TICKER × DATE presence grid for THAT group — one row per
    ticker, one box per date, shaded by provenance (both / stream / backfill / absent). Lazy (only on a cell
    click) + paginated (``limit`` rows, ranked most-covered first; the universe is ~7.3k). 404 for an unknown
    group. ``days`` sets the date window.

    SERVED FROM the same Redis cache the worker precomputes (a blob per group) — sub-ms, off the request path.
    A cold cache / unreachable Redis returns a ``warming`` drill (empty but valid) rather than the ~1.5s live
    build. ``refresh=1`` forces a live in-process build (the manual escape hatch when the worker is down)."""
    if refresh:
        try:
            return JSONResponse(GLIMPSE_CACHE.drill(group, STORE_ROOT, days=days, limit=limit, force=True))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown feature group: {group}")
    return JSONResponse(read_glimpse_drill(group, days=days, limit=limit))


@app.get("/api/store-grid/matrix")
def store_grid_matrix() -> Response:
    """The ALWAYS-WARM ticker×date coverage matrix — the React grid's data feed. DATE rows (newest first,
    ~18 months back) × TICKER columns (the captured universe, default-sorted most-covered first); each cell
    a coverage byte (0..255 = proportion of the feature store present for that ticker that date) plus a binary
    trust bit (1 = every present group fully-trusted).

    Served straight from the ``store-glimpse-worker``'s precomputed Redis blob, ALREADY gzip-compressed — the
    bytes are passed through with ``Content-Encoding: gzip`` (a dense ~2.8M-cell matrix is multi-MB raw JSON,
    a few hundred KB gzipped), so there is no build and no recompress on this request. On the genuine
    first-ever boot (worker has not written yet) or unreachable Redis, returns 503 with a small ``booting``
    JSON the UI shows as a brief one-time loading state — NOT the old recurring "warming…" placeholder.

    Shape (decompressed; see store_grid.build_store_grid):
      {generated_at, anchor_date, lookback_days, n_groups, n_trusted_groups,
       dates: [...], tickers: [...], coverage: [[byte,...],...], trusted: [[bit,...],...],
       coverage_pct: [...], summary: {...}}
    """
    blob = read_grid_gzip()
    if blob is None:
        return JSONResponse(
            {"booting": True, "detail": "coverage matrix not built yet (first boot)"},
            status_code=503,
        )
    return Response(
        content=blob,
        media_type="application/json",
        headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"},
    )


@app.get("/api/store-grid/meta")
def store_grid_meta_json() -> JSONResponse:
    """The small matrix meta header for the UI's "as of HH:MM:SS" staleness + dims — generated_at, anchor,
    n_dates/n_tickers/n_groups, gzip size, build seconds. ``booting`` until the worker's first write lands.
    """
    meta = read_grid_meta()
    if meta is None:
        return JSONResponse({"booting": True}, status_code=503)
    return JSONResponse(meta)


@app.get("/api/store-grid/ticker/{symbol}")
def store_grid_ticker_drill(symbol: str) -> JSONResponse:
    """Drill for one TICKER column: its per-(date × group) presence + per-group binary trust — what a cell
    click opens. Served from the worker's pre-warmed blob for the most-covered tickers; an un-warmed ticker
    falls back to a cheap one-ticker live build."""
    return JSONResponse(read_grid_drill(symbol))


@app.get("/api/scorecard")
def scorecard_json(refresh: bool = False) -> JSONResponse:
    """SYSTEM PROGRESS scorecard: Ben's six platform axes (A trusted / B deployed / C trust-process health /
    D latency / E raw-coverage / F open issues), computed read-only from the existing tables/manifests/doc/gh.

    A, C, and the defect/quarantine side of F come from ONE ``feature_trust`` x ``feature_parity_defect`` read
    (the trust frontier); B from the live bus schema (the deployed fingerprint set); D from the documented
    end-to-end latency baseline; E from the raw manifests; F's open-PR count from ``gh``. Building through the
    cache APPENDS a headline snapshot to the append-only time series (de-duped per UTC minute) so the panel can
    draw each axis's trajectory. ``refresh=1`` bypasses the TTL cache (and writes a fresh snapshot).

    Shape (see docs/SCORECARD.md):
      {generated_at, axes: {A_trusted, B_deployed, C_process_health, D_latency, E_raw_coverage,
       F_open_issues}, snapshot}
    """
    return JSONResponse(SCORECARD_CACHE.scorecard(STORE_ROOT, force=refresh))


@app.get("/api/scorecard/history")
def scorecard_history_json() -> JSONResponse:
    """The persisted scorecard SNAPSHOT time series, OLDEST-FIRST (what the panel's sparklines draw from).

    Shape: [{ts, axes: {A_trusted, B_deployed, C_process_health, D_latency, E_raw_coverage, F_open_issues}}, ...]
    where each axis holds only the headline scalar(s) the trend line needs (see scorecard_store)."""
    return JSONResponse(scorecard_store.read_snapshots())


@app.get("/scorecard", response_class=HTMLResponse)
def scorecard_page() -> str:
    """The visual system-progress scorecard (vanilla HTML/JS; fetches /api/scorecard + history client-side)."""
    return SCORECARD_HTML


@app.get("/api/liquidity-bands")
def liquidity_bands_json(
    days: int = 90, cuts: str | None = None, asof: str | None = None, refresh: bool = False
) -> JSONResponse:
    """Canonical ADV-rank / liquidity-band reference surface — the trailing-20d dollar-volume RANK over the
    raw bars that every research lane otherwise re-derives ad hoc (Lane C's bands, FeatureInventor top-400,
    the pilot top-500). Per (symbol, date) point-in-time: trailing-20d ADV, its cross-sectional rank, and a
    band label under configurable contiguous cuts.

    Returns band SIZES over time, membership STABILITY (day-to-day band-cross turnover), and an as-of
    snapshot of band membership. ``cuts`` overrides the default rank cuts (``500,1000,2000,4000`` ==
    Lane C's adjudicated B1-B5); ``days`` clips the timeline (0 = full history); ``asof`` pins the snapshot
    to a date (point-in-time); ``refresh=1`` bypasses the TTL cache.

    Shape:
      {generated_at, store_root, cuts, band_labels, adv_window, min_trailing_days, days,
       earliest, latest, asof, n_dates, n_ranked_symbols,
       timeline: [{date, total, bands: {label: n}}],
       stability: {overall_cross_rate, per_band: {label: {pairs, crosses, cross_rate}}, n_transitions},
       snapshot: {date, bands: {label: {n, rank_lo, rank_hi, min_adv, max_adv, median_adv, members_sample}}}}
    """
    try:
        parsed_cuts = parse_cuts(cuts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(BANDS_CACHE.bands(STORE_ROOT, cuts=parsed_cuts, days=days, asof=asof, force=refresh))


@app.get("/api/liquidity-bands/symbol/{symbol}")
def liquidity_bands_symbol_json(symbol: str, cuts: str | None = None, refresh: bool = False) -> JSONResponse:
    """One symbol's ADV-rank / band history over time — its trailing ADV, cross-sectional rank, and band on
    each ranked date. The "given a symbol, its ADV-rank history" lookup."""
    try:
        parsed_cuts = parse_cuts(cuts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(symbol_history(symbol.upper(), STORE_ROOT, cuts=parsed_cuts, force=refresh))


@app.get("/api/liquidity-bands/members/{band}")
def liquidity_bands_members_json(
    band: str, cuts: str | None = None, asof: str | None = None, refresh: bool = False
) -> JSONResponse:
    """The full current (or ``asof``-date) membership of one band, each member's rank + trailing ADV — the
    reproducible-universe export a lane uses instead of an ad-hoc top-N (band "2000-4000" == Lane C's B4)."""
    try:
        parsed_cuts = parse_cuts(cuts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(band_members(band, STORE_ROOT, cuts=parsed_cuts, asof=asof, force=refresh))


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
        "<span class='ok'>● connected</span>" if metrics["db_ok"] else "<span class='bad'>● down</span>"
    )

    rows_html = ""
    for table, info in metrics.get("tables", {}).items():
        latest = info.get("latest") or "—"
        rows_html += f"<tr><td>{table}</td><td class='num'>{info['rows']:,}</td>" f"<td>{latest}</td></tr>"

    recon = metrics.get("last_reconciliation")
    recon_html = (
        "no reconciliation yet" if not recon else (f"{'OK' if recon['ok'] else 'MISMATCH'} @ {recon['ts']}")
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
        cov_html = (
            "<h2 style='margin-top:0;font-size:15px;'>Coverage</h2><p class='muted'>no coverage data yet</p>"
        )

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
<a href="/scorecard" style="color:#58a6ff;text-decoration:none;font-size:13px;">Progress scorecard &rarr;</a> &nbsp;
<a href="/status" style="color:#58a6ff;text-decoration:none;font-size:13px;">Hourly status &rarr;</a> &nbsp;
<a href="/jobs" style="color:#58a6ff;text-decoration:none;font-size:13px;">Jobs &rarr;</a> &nbsp;
<a href="/progress" style="color:#58a6ff;text-decoration:none;font-size:13px;">Progress reports &rarr;</a> &nbsp;
<a href="/store-grid/" style="color:#58a6ff;text-decoration:none;font-size:13px;">Store coverage grid &rarr;</a> &nbsp;
<a href="/raw-coverage" style="color:#58a6ff;text-decoration:none;font-size:13px;">Raw-tape coverage &rarr;</a> &nbsp;
<a href="/liquidity-bands" style="color:#58a6ff;text-decoration:none;font-size:13px;">Liquidity bands &rarr;</a> &nbsp;
<a href="/sector-coverage" style="color:#58a6ff;text-decoration:none;font-size:13px;">Sector coverage &rarr;</a> &nbsp;
<a href="/universe-coverage" style="color:#58a6ff;text-decoration:none;font-size:13px;">Universe coverage &rarr;</a></h1>
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


# The React store-coverage grid SPA (services/dashboard/frontend), built to static assets by the Dockerfile's
# node stage into /app/frontend/store-grid. Mounted LAST (after every /api/* route is declared) at /store-grid
# with html=True so index.html serves at the mount root and client-side asset paths resolve. The /api/store-grid/*
# JSON routes are unaffected — they live under /api, a different prefix, and are matched by their explicit routes
# before this mount. STATICFILES_DIR is overridable; if the build is absent (e.g. a non-Docker dev run that
# skipped `npm run build`), the mount is simply skipped so the rest of the dashboard still boots.
STATICFILES_DIR = Path(os.environ.get("STORE_GRID_STATIC_DIR", "/app/frontend/store-grid"))
if STATICFILES_DIR.is_dir():
    app.mount("/store-grid", StaticFiles(directory=STATICFILES_DIR, html=True), name="store-grid")
