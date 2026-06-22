"""Quant dashboard — the feature-store COVERAGE GRID, plus two read-only sibling tabs.

The dashboard centers on the always-warm ticker×date feature-store coverage grid (the React SPA in
``services/dashboard/frontend``), served at the ROOT ``/``. Everything that used to clutter the dashboard
(status board, jobs, scorecard, progress reports, raw/sector/universe coverage, liquidity bands, the old
DB-health home page) was removed — both the UI pages and the now-dead ops-introspection read routes
(``/api/status/rows``, ``/api/scorecard``, ``/api/scorecard/history``, ``/api/jobs``) and their backing
modules. Two read-only TABS sit alongside the grid in the same SPA: the latency-expectations view
(``/api/latency-expectations``) and the News & Filings view (``/api/news-edgar/*`` — live stream rate +
store composition) and the hourly Status view (``/api/status-grid`` read + ``/api/status-grid/reaction``
write — the Lead-owned hour×workstream Progress/Blockers table + Ben's per-row reaction box, persisted to an
append-only JSON store shared with the host Lead loop). All read-side w.r.t. the feature store; the surface is
the four grid routes + latency + the two news/edgar routes + the two status routes + ``/healthz``.

The grid's data is precomputed by the ``store-grid-worker`` container into MongoDB on a 10-minute schedule and
served here from that cache, so a page load is one indexed document fetch and the heavy ~3-4min build is never
on the request path. The only loading state the UI ever shows is the genuine first-ever boot before the first
build lands (the API returns 503 ``booting``); there is no recurring "warming".

CACHE DISCIPLINE: the SPA's ``index.html`` is served ``no-cache`` (must-revalidate) so a browser always
re-fetches the HTML and never pins a stale build's JS-bundle hash (the cached-old-index → 404-on-new-hash →
blank-page stall this fixes). The content-hashed ``/assets/*`` are immutable, so they keep a long cache. NO
feature-store schema/format/fingerprint change — this service is read-side only.
"""

import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from latency_expectations import load_latency_expectations
from lifecycle_state import lifecycle_snapshot
from news_edgar import composition_snapshot, stream_snapshot
from pydantic import BaseModel, Field
from status_grid import append_reaction
from status_grid import read_grid as read_status_grid
from store_grid_cache import read_drill as read_grid_drill
from store_grid_cache import read_grid_gzip
from store_grid_cache import read_meta as read_grid_meta

app = FastAPI(title="Quant Coverage Grid")

# The HTML must never be cached: a rebuilt image emits a new content-hashed JS bundle, and a browser holding
# the OLD index.html would keep requesting the OLD hash (404 → blank page). must-revalidate forces a re-fetch.
NO_CACHE_HEADERS = {"Cache-Control": "no-cache, max-age=0, must-revalidate"}


@app.get("/api/store-grid/matrix")
def store_grid_matrix() -> Response:
    """The ALWAYS-WARM date × feature-group coverage matrix — the grid's data feed. DATE rows (newest first,
    ~18 months back) × GROUP columns (the ~63 registry feature-groups, trusted-first); each cell a coverage
    byte (0..255 = fraction of that date's captured tickers that have this group) and the per-column trust bit.

    Served straight from the worker's precomputed MongoDB document, ALREADY gzip-compressed — the bytes are
    passed through with ``Content-Encoding: gzip`` (no build, no recompress on the request path). On the genuine
    first-ever boot (worker has not written yet) or unreachable Mongo, returns 503 with a small ``booting`` JSON
    the UI shows as a brief one-time loading state — NOT a recurring "warming" placeholder.
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
    n_dates/n_groups, gzip size, build seconds. ``booting`` until the worker's first write lands."""
    meta = read_grid_meta()
    if meta is None:
        return JSONResponse({"booting": True}, status_code=503)
    return JSONResponse(meta)


@app.get("/api/store-grid/cell")
def store_grid_cell_drill(group: str, date: str) -> JSONResponse:
    """Drill for one (date × group) CELL: the per-TICKER breakdown — which tickers have that group's features
    on that date (ranked, paginated), plus the date's captured-universe size + coverage %. Served from the
    worker's pre-warmed cell doc; a cold/unreachable cache returns an empty-but-valid drill."""
    return JSONResponse(read_grid_drill(group, date))


@app.get("/api/latency-expectations")
def latency_expectations() -> JSONResponse:
    """The per-group feature-latency expectations (#321) for the latency view — the slowest-first
    ``compute_latest`` profile plus the e2e bar->vector context header. Served straight from the JSON baked
    into the image (``docs/feature_latency_expectations.json``); the UI renders ``groups`` as a slowest-first
    table. Returns 503 ``booting`` if the artifact is absent (mirrors the grid's first-boot state)."""
    data = load_latency_expectations()
    if data is None:
        return JSONResponse(
            {"booting": True, "detail": "latency expectations artifact not present"},
            status_code=503,
        )
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/news-edgar/stream")
def news_edgar_stream() -> JSONResponse:
    """The LIVE STREAMING panel for the News & Filings tab — current articles/min + filings/min, a recent
    per-minute timeline, and ACTIVE/WARN/STALE freshness per source (business-hours-aware, matching the
    data_freshness cron alert). Cheap and uncached: a few-ms recent-window filings query + the newest news
    partition scan. ``no-store`` so the UI always sees the live rate, never a cached one."""
    return JSONResponse(stream_snapshot(), headers={"Cache-Control": "no-store"})


@app.get("/api/news-edgar/composition")
def news_edgar_composition() -> JSONResponse:
    """The STORE COMPOSITION panel for the News & Filings tab — total articles + span + top symbols, total
    filings + span + per-form-type breakdown, and the feature status (edgar_filing_frequency LIVE; news
    sentiment/hotness COMING). Served from a short-TTL in-process cache (the heavy ~3M-row filings aggregates
    change slowly), so the scan never lands repeatedly on the request path."""
    return JSONResponse(composition_snapshot(), headers={"Cache-Control": "no-store"})


class ReactionBody(BaseModel):
    """The POST body for Ben's reaction to an hour's status row. ``hour`` is the row id
    (``YYYY-MM-DDTHH:00Z``); ``reaction`` is the free text (empty clears the reaction)."""

    hour: str = Field(..., min_length=1)
    reaction: str = ""


@app.get("/api/status-grid")
def status_grid() -> JSONResponse:
    """The HOURLY STATUS TABLE (docs/OPERATING_MODEL.md §"The hourly status dashboard") — the eight workstream
    columns + every hourly row (newest first), each cell a concise Progress + Blockers and each row Ben's
    reaction. Read straight from the append-only JSON store the Lead's conductor loop writes on the host
    (shared via the ~/.quant-ops bind-mount); returns an empty-but-valid table on the genuine first boot
    before the first row is synthesized. ``no-store`` so Ben always sees the latest synthesized hour."""
    return JSONResponse(read_status_grid(), headers={"Cache-Control": "no-store"})


@app.post("/api/status-grid/reaction")
def status_grid_reaction(body: ReactionBody) -> JSONResponse:
    """Record Ben's reaction to an hour's status row — the input box's WRITE path. Append-only w.r.t. the
    store (replaces only that row's reaction, never touches the Lead-synthesized cells), and the Lead reviews
    these every cycle. Returns the updated row."""
    row = append_reaction(body.hour, body.reaction)
    return JSONResponse(row, headers={"Cache-Control": "no-store"})


@app.get("/api/lifecycle-state")
def lifecycle_state() -> JSONResponse:
    """The per-group CERTIFICATION-LIFECYCLE state — makes the now-running within-day parity lifecycle legible
    (docs/WITHIN_DAY_PARITY_CERTIFICATION.md). Each feature-group's FURTHEST stage on the staged progression
    UNVERIFIED → MONITORING → CERTIFIED → TRUSTED, read off the live ``within_day_assignment`` (who owns the
    monitoring lock), ``within_day_parity_cert`` (the latest within-day intraday-parity verdict +
    stable_cycles/value_rate/cert_day), and ``feature_trust`` (the permanent binary TRUSTED grant), joined to
    groups via the registry catalog. Three small indexed queries, short-TTL cached off the request path;
    ``no-store`` so Ben always sees the latest cycle. Returns 503 ``booting`` if the trust DB is unreachable
    (mirrors the grid's first-boot state) rather than 500-ing the page."""
    try:
        return JSONResponse(lifecycle_snapshot(), headers={"Cache-Control": "no-store"})
    except psycopg.OperationalError as exc:
        return JSONResponse(
            {"booting": True, "detail": f"trust DB unreachable: {exc}"},
            status_code=503,
        )


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe — the process is up and serving. Does not touch Mongo / the store (the grid routes report
    their own booting state), so a healthy container is never gated on a cold cache."""
    return JSONResponse({"ok": True})


@app.get("/feature-grid")
def feature_grid_redirect() -> RedirectResponse:
    """``/feature-grid`` is Ben's canonical bookmark for the coverage grid. The grid now lives at the root ``/``,
    so this permanently redirects there — the old URL must NEVER 404. Registered before the root SPA routes so
    it is matched as an explicit route."""
    return RedirectResponse(url="/", status_code=308)


@app.get("/favicon.ico")
def favicon() -> Response:
    """The SPA ships no favicon, so a browser's ``/favicon.ico`` request would otherwise fall through to the SPA
    fallback (a 200 of HTML the browser can't render as an icon). Return a quiet 204 to silence the request.
    """
    return Response(status_code=204)


# The React coverage-grid SPA (services/dashboard/frontend), built to static assets by the Dockerfile's node
# stage into /app/frontend/store-grid. The content-hashed bundle lives under /assets — mounted as StaticFiles
# so those immutable files are served directly (and may be long-cached by the browser). index.html itself is
# served by the explicit routes below with NO_CACHE_HEADERS so a rebuild's new bundle hash is always picked up.
# STATICFILES_DIR is overridable; if the build is absent (a non-Docker dev run that skipped ``npm run build``),
# the mount + SPA routes are skipped so the API still boots.
STATICFILES_DIR = Path(os.environ.get("STORE_GRID_STATIC_DIR", "/app/frontend/store-grid"))
INDEX_HTML = STATICFILES_DIR / "index.html"

if STATICFILES_DIR.is_dir():
    assets_dir = STATICFILES_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="grid-assets")

    @app.get("/")
    def spa_root() -> FileResponse:
        """The SPA shell at the root. Served ``no-cache`` so the browser re-validates the HTML on every load and
        never pins a previous build's bundle hash."""
        return FileResponse(INDEX_HTML, headers=NO_CACHE_HEADERS)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        """Client-side-routing fallback: any non-API, non-asset path returns the SPA shell (``no-cache``), so a
        deep link or refresh resolves to the same always-revalidated index.html. Declared LAST, after every
        ``/api/*`` route and the ``/assets`` mount, so those are matched first."""
        return FileResponse(INDEX_HTML, headers=NO_CACHE_HEADERS)
