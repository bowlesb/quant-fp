# Store grid — date × feature-group coverage matrix (always-warm)

The **immediate glimpse into the feature store**: one fast, always-warm grid whose **rows are DATES** (most
recent at top, ~18 months back) and **columns are the ~63 FEATURE GROUPS** (the registry groups). Each **cell**
encodes, as darkness, the **fraction of that date's captured tickers that have this feature-group** — an
all-ticker aggregate per group per date. This fits one screen and is legible (392 × 63 ≈ 25k cells); the
earlier per-ticker axis (11k columns against a shallow store) read as a near-empty black void and was rejected.
A binary trust overlay colours whole group columns (a group is trusted-or-not).

**This grid IS the dashboard.** It is served at the ROOT `/`; every other dashboard page (status, jobs,
scorecard, progress, raw/sector/universe coverage, liquidity bands, the old DB-health home) has been removed
as UI. The matrix is built by a **permanent background worker** that refreshes it into **MongoDB every 10
minutes**, and the dashboard serves the last-good document — so a refresh is one indexed Mongo fetch and the
heavy build is never on the request path. The only loading state a reader ever sees is the genuine first-ever
boot (the API returns `503 {booting}`); there is no recurring "warming".

## What a cell means

```
coverage[group][date] = (# in-universe tickers that have this GROUP on this date)
                        / (# tickers captured at all that date)
```

The denominator is the **captured universe that date** (the union of tickers across all groups on that date),
so a universe-wide bar group reads ~full and a thin order-flow group reads faint — an honest per-group breadth.
A far-back date where only the calendar groups backfill shows a couple of full columns and the rest blank.
Coverage is quantized to a byte (0..255). Store depth is genuinely uneven — only the calendar groups go back
~18 months, most groups are shallow (recent weeks/months) — so the faint/blank far-back rows are **honest
sparsity, not a bug**.

**Binary trust** (trusted vs untrusted — nothing else). Trust is a per-FEATURE property
(`feature_trust.trust_state = 'TRUSTED'`, the binary system of record); a GROUP is trusted iff **all** its
features are. Since the columns ARE groups, trust colours whole columns — the 6 trusted vs 57 untrusted are
immediately visible. Columns are ordered **trusted-first** so the trusted groups cluster on the left.

## Architecture

* **Builder** — `services/dashboard/store_grid.py`. `gather_window()` does the single store-reading pass
  (one bounded evenly-sampled symbol-set read per in-window partition, reusing `feature_grid._read_symbols`);
  `build_store_grid()` rolls that up into the date × group aggregate matrix, `build_cell_drill()` derives one
  (group, date) cell's per-ticker list — both purely in-memory from the same gather. Read-side only; no store
  schema/format/fingerprint change.
* **Worker** — `services/dashboard/store_grid_cache.py`, run as the **`store-grid-worker`** compose service
  (`restart: unless-stopped`, built from the dashboard image, mounts `fp_store_real:/store:ro`, reaches the
  `mongo` service on `quant_default`). A permanent loop: build on boot, write the gzip-compressed matrix doc +
  one drill doc per populated `(group, date)` cell + a meta doc to **MongoDB**, then sleep **10 minutes** and
  repeat. Each loop OVERWRITES the docs (no TTL — they persist until the next build replaces them), so the
  last-good document always serves between builds. A full 18-month rebuild measures ~2.5 min (dominated by the
  deep calendar groups); 10-minute cadence is plenty fresh (capture is per-session, trust per-sweep).
* **Cache** — a dedicated **`mongo`** compose service (`mongo:7`, a small `mongo_data` volume, LAN-internal,
  no published port). NOT the feature pipeline's store — it holds only the precomputed dashboard grid.
* **Reader** — `services/dashboard/app.py` routes below. One indexed Mongo `find_one`; the matrix route passes
  the stored gzip bytes straight through with `Content-Encoding: gzip` (the 392 × 63 matrix is ~4 KB gzipped —
  no build, no recompress on the request path).

## URLs

| URL | What |
|---|---|
| `GET /api/store-grid/matrix` | the packed date × group coverage matrix + per-column trust (gzip; the React grid's feed). `503 {booting}` only before the worker's first write. |
| `GET /api/store-grid/meta` | small header: `generated_at`, `anchor_date`, dims, gzip size, build seconds — the UI's "as of HH:MM:SS" staleness. |
| `GET /api/store-grid/cell?group=<g>&date=<d>` | one `(date × group)` cell's per-TICKER breakdown — which tickers have that group that date (ranked, capped) + the date's universe size + coverage %. The cell-click drill. |

## Matrix JSON shape (decompressed)

```jsonc
{
  "generated_at": "2026-06-20T...Z",
  "store_root": "/store",
  "anchor_date": "2026-06-18",
  "lookback_days": 548,
  "n_groups": 63,
  "n_trusted_groups": 6,
  "dates":   ["2026-06-18", "2026-06-17", ...],   // rows, newest first, WEEKDAYS only
  "groups":  ["calendar", ...],                    // columns, trusted-first then alphabetical
  "group_trusted": [1, 0, ...],                    // per-column binary trust bit (aligned to groups)
  "coverage": [[byte, ...], ...],                  // rows ⟂ dates, cols ⟂ groups; 0..255
  "universe": [n, ...],                            // per-date captured-universe size (the denominator)
  "group_coverage_pct": [...],                     // per-group mean coverage over present dates
  "legend": { "coverage_scale": "...", "trust_overlay": "...", "depth_note": "..." },
  "summary": { "n_dates": ..., "n_groups": ..., "n_trusted_groups": ..., "mean_coverage_pct": ... }
}
```

Rows are **weekdays only** (weekend rows never capture, so dropping them keeps the matrix ~30% tighter; a
weekday with no data still renders blank — honest, it *was* a trading day).

## Drill-down visual nesting (Ben's explicit ask)

When the user clicks a cell and it **expands** to show detail (the per-ticker breakdown for that group+date),
it must be **visually unmistakable that the expanded content belongs to the thing that was clicked**. The drill
makes the parent→child relationship obvious by combining:

- a **labeled header chip** on the panel naming its parent (group + date + trust) and the count;
- **indentation / a contained card** so the ticker list visibly sits *inside* the panel;
- a **distinct background shade** for the nested region so the boundary is unmistakable;
- **tighter / smaller child chips** (the monospace ticker grid) so they read as detail under the summary.

Implemented in `DrillPanel.tsx`: the header chip names the parent group + a trust pill + the date; a summary
line gives "N of M captured tickers · X% coverage"; the per-ticker list sits inside a further-indented,
distinctly-shaded nested card whose own header chip names *its* parent (the group) — the same treatment one
level down.

## React SPA (the whole dashboard)

The grid UI is a Vite + React + TypeScript SPA in `services/dashboard/frontend/`, built to static assets by
the Dockerfile's `webbuild` (node) stage and served by the dashboard FastAPI app at the **ROOT `/`** (a
`StaticFiles` mount, `html=True`). The grid IS the dashboard — there is no other page. Components:

- **`CanvasHeatmap.tsx`** — a **canvas** renderer (never DOM-per-cell): dates down the rows (newest at top),
  the ~63 feature-GROUP columns across the top (angled name headers, trusted-first), cell darkness = coverage,
  consuming `/api/store-grid/matrix` (gzip pass-through). The group columns all fit one screen; the date axis
  scrolls vertically (row-windowed). Hover → tooltip; click a populated cell → the per-ticker drill; a search
  highlights a group column.
- **`App.tsx`** — fetches the matrix + polls `/api/store-grid/meta` for the **"as of HH:MM:SS"** staleness,
  re-pulling the matrix only when a newer build exists. The binary-trust overlay is a single toggle (trusted
  green / untrusted grey — no other states). The **only** loading state is the genuine first-ever boot (the
  API's `503 {booting}`); there is no recurring "warming".
- **`Tooltip.tsx`** / **`DrillPanel.tsx`** — the hover readout and the nested drill described above.

### Stripped-down dashboard surface

This PR strips the dashboard to ONLY the grid. **Deleted** (UI-only page modules + their routes): `status_page`,
`scorecard_page`, `raw_coverage_page`, `sector_coverage_page`, `sector_coverage`, `universe_coverage_page`,
`universe_coverage`, `liquidity_bands_page`, `liquidity_bands`, `store_glimpse`, `store_glimpse_cache`, and the
old `/`, `/status`, `/jobs`, `/scorecard`, `/progress`, `/raw-coverage`, `/sector-coverage`,
`/universe-coverage`, `/liquidity-bands` HTML routes + the page-only `/api/*` routes for them. **Kept** (still
written by the host Lead loop + crons — ops continuity, not UI): `status_store.py`, `scorecard.py` +
`scorecard_store.py`, `raw_coverage.py` (a `scorecard` dependency), `feature_grid.py` (the store introspection
the worker + scorecard reuse), and `jobs_page.load_status`. The **remaining API** is the grid routes
(`/api/store-grid/*`), `/healthz`, and three ops-introspection READ routes (`/api/status/rows`,
`/api/scorecard[/history]`, `/api/jobs`) backed by the live JSON stores.

## Deployment

A single clean cutover: `docker compose up -d --build mongo store-grid-worker dashboard`. The `mongo` service
comes up, the `store-grid-worker` builds the first matrix into it (confirm its log shows a matrix write), and
the dashboard serves the grid at `/`. The previous Redis-backed `store-glimpse-worker` and the old host cron
`ops/collect_store_glimpse.py` (the `1-58/3 * * * *` crontab line) are obsolete — remove that crontab line on
deploy (`crontab -e`); see the cron registry in `docs/OPERATIONS.md`.
