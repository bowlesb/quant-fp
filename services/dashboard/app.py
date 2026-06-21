"""Quant dashboard — the feature-store COVERAGE GRID, and nothing else.

The dashboard is deliberately a SINGLE thing: the always-warm ticker×date feature-store coverage grid (the
React SPA in ``services/dashboard/frontend``), served at the ROOT ``/``. Everything that used to clutter the
dashboard (status board, jobs, scorecard, progress reports, raw/sector/universe coverage, liquidity bands, the
old DB-health home page) has been removed — both the UI pages and the now-dead ops-introspection read routes
(``/api/status/rows``, ``/api/scorecard``, ``/api/scorecard/history``, ``/api/jobs``) and their backing
modules. The grid is the one view that matters; the surface is now exactly four grid routes + ``/healthz``.

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

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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
