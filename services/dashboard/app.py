"""Quant dashboard — the feature-store COVERAGE GRID, and nothing else.

The dashboard is deliberately a SINGLE thing: the always-warm ticker×date feature-store coverage grid (the
React SPA in ``services/dashboard/frontend``), served at the ROOT ``/``. Everything that used to clutter the
dashboard (status board, jobs, scorecard, progress reports, raw/sector/universe coverage, liquidity bands, the
old DB-health home page) has been removed as UI — the grid is the one view that matters.

The grid's data is precomputed by the ``store-grid-worker`` container into MongoDB on a 10-minute schedule and
served here from that cache, so a page load is one indexed document fetch and the heavy ~3-4min build is never
on the request path. The only loading state the UI ever shows is the genuine first-ever boot before the first
build lands (the API returns 503 ``booting``); there is no recurring "warming".

A few OPS-INTROSPECTION read routes remain (``/api/status/rows``, ``/api/scorecard``, ``/api/scorecard/history``,
``/api/jobs``): their backing JSON stores are still written by the host Lead loop + crons, so these read-only
endpoints stay as harmless curl-able ops visibility — they have no dashboard page. ``/healthz`` is the
container health check. NO feature-store schema/format/fingerprint change — this service is read-side only.
"""

import os
from pathlib import Path

import scorecard_store
import status_store
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jobs_page import load_status as load_jobs_status
from scorecard import CACHE as SCORECARD_CACHE
from store_grid import STORE_ROOT
from store_grid_cache import read_drill as read_grid_drill
from store_grid_cache import read_grid_gzip
from store_grid_cache import read_meta as read_grid_meta

app = FastAPI(title="Quant Coverage Grid")

# index.html is served with NO-CACHE so it always revalidates. The Vite bundle is content-hashed (its filename
# changes every rebuild), so a browser holding a CACHED index.html would keep requesting the OLD hash after we
# rebuild the image → 404 on the dead /assets/*.js → blank/stalled page. Forcing the HTML to revalidate (while
# the hashed /assets/* keep their long immutable cache) breaks that stall: the browser always re-fetches the
# current index.html, which points at the live hashes.
INDEX_NO_CACHE = "no-cache, max-age=0, must-revalidate"
# The hashed /assets/* are content-addressed (the filename changes when the content changes), so a cached copy
# is never stale — cache them hard for a year as immutable.
ASSET_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles that stamps a long immutable Cache-Control on every hit. Safe because Vite emits
    content-hashed asset filenames, so the bytes behind a given URL never change."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = ASSET_IMMUTABLE_CACHE
        return response


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
    so this permanently redirects there — the old URL must NEVER 404. Registered before the root StaticFiles
    mount so it is matched as an explicit route."""
    return RedirectResponse(url="/", status_code=308)


@app.get("/api/status/rows")
def status_rows_json() -> JSONResponse:
    """OPS read-only: the hourly status snapshots NEWEST-FIRST, from the append-only status store the host Lead
    loop writes. No dashboard page renders this anymore — it stays as curl-able ops visibility.

    Shape: [{ts, cells: {workstream: {progress, blockers}}, reaction}, ...] (see status_store).
    """
    return JSONResponse(status_store.read_rows())


@app.get("/api/scorecard")
def scorecard_json(refresh: bool = False) -> JSONResponse:
    """OPS read-only: the system-progress scorecard (Ben's six platform axes), computed read-only from the
    existing tables/manifests/doc/gh. Building through the cache appends a headline snapshot to the append-only
    time series (the same store the Lead loop reads). ``refresh=1`` bypasses the TTL cache. No dashboard page.
    """
    return JSONResponse(SCORECARD_CACHE.scorecard(STORE_ROOT, force=refresh))


@app.get("/api/scorecard/history")
def scorecard_history_json() -> JSONResponse:
    """OPS read-only: the persisted scorecard SNAPSHOT time series, OLDEST-FIRST (the trajectory the Lead loop
    and any external monitor read). Shape: [{ts, axes: {...}}, ...] (see scorecard_store)."""
    return JSONResponse(scorecard_store.read_snapshots())


@app.get("/api/jobs")
def jobs_json() -> JSONResponse:
    """OPS read-only: the jobs status (scheduled crons, running job containers, recent runs) that
    ``ops/collect_jobs_status.py`` writes on the host. Returns an empty-but-valid shape if the collector has
    not written ``jobs_status.json`` yet. No dashboard page renders this anymore."""
    data = load_jobs_status()
    if data is None:
        data = {"scheduled": [], "running": [], "recent_runs": [], "collected_at": None}
    return JSONResponse(data)


# The React coverage-grid SPA (services/dashboard/frontend), built to static assets by the Dockerfile's node
# stage into /app/frontend/store-grid. STATICFILES_DIR is overridable; if the build is absent (a non-Docker dev
# run that skipped ``npm run build``), the mounts/routes are skipped so the API still boots.
#
# Serving is split so the HTML revalidates but the hashed assets cache hard:
#   * ``/assets/*`` (Vite's content-hashed JS/CSS) → StaticFiles with a LONG immutable cache; the filename
#     changes every rebuild, so a stale copy is never wrong.
#   * ``/`` and any SPA deep link → ``index.html`` with NO-CACHE, so the browser always re-fetches the HTML and
#     picks up the live asset hashes (the rebuild-stall fix).
STATICFILES_DIR = Path(os.environ.get("STORE_GRID_STATIC_DIR", "/app/frontend/store-grid"))
INDEX_HTML = STATICFILES_DIR / "index.html"
ASSETS_DIR = STATICFILES_DIR / "assets"


def _index_response() -> FileResponse:
    """index.html with the no-cache header — the SPA entry + deep-link fallback."""
    return FileResponse(INDEX_HTML, headers={"Cache-Control": INDEX_NO_CACHE})


if STATICFILES_DIR.is_dir():
    if ASSETS_DIR.is_dir():
        # Content-hashed bundles: immutable, cache for a year. New builds emit new filenames.
        app.mount(
            "/assets",
            ImmutableStaticFiles(directory=ASSETS_DIR),
            name="grid-assets",
        )

    @app.get("/")
    def spa_root() -> FileResponse:
        """The grid IS the dashboard — index.html at the root, always revalidated (no-cache)."""
        return _index_response()

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        """SPA deep-link fallback: any non-API, non-asset path returns the no-cache index.html so client-side
        routing resolves. Declared LAST so every explicit ``/api/*`` route and the ``/assets`` mount win. A
        request for a missing real file (e.g. ``/favicon.ico`` when none is shipped) 404s rather than returning
        HTML with a 200."""
        candidate = STATICFILES_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        if "." in Path(full_path).name:
            # Looks like a static file request (has an extension) but no such file exists — a real 404, not an
            # SPA route. Returning index.html here would mask missing assets as 200s.
            raise HTTPException(status_code=404)
        return _index_response()
