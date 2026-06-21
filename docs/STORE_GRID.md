# Store grid — date × column coverage matrix (always-warm, v3)

The **immediate glimpse into the feature store**: one fast, always-warm grid whose **rows are DATES** (most
recent at top, ~18 months back) and **columns are the RAW tape layers (bars / trades / quotes) followed by the
~63 FEATURE GROUPS**. Each **cell** darkens with the **fraction of the full universe that has this column's data
on that date**. The grid is a **light theme**: WHITE = zero coverage = the page background, so an absent cell
and a zero-coverage cell look identical. Coverage darkens each cell toward its column's colour — a **trusted**
feature group → dark BLUE, an **untrusted** group → dark RED, a **raw** layer → dark SLATE — so trust is always
shown in the colour (there is no toggle).

**This grid IS the dashboard.** Served at the ROOT `/`; every other dashboard page has been removed as UI. The
matrix is built by a **permanent background worker** that refreshes it into **MongoDB every 10 minutes**; the
dashboard serves the last-good document (one indexed Mongo fetch, the heavy build never on the request path).
The only loading state a reader ever sees is the genuine first-ever boot (`503 {booting}`); no recurring
"warming".

## What a cell means

```
coverage[column][date] = (# tickers that have this column's data on this date) / UNIVERSE_SIZE
```

The denominator is a **single fixed number** — the latest session's in-universe ``universe_membership`` count
(~7.3k), applied identically to every raw layer, every feature group, and every individual feature (one fixed
reference, surfaced in the legend so it is clear what 100% means). A universe-wide bars layer / bar group reads
~full; a thin order-flow group reads faint; a far-back date where only the calendar groups backfill shows a
couple of full columns and the rest white. Coverage is quantized to a byte (0 = none, 255 = the whole universe).
Far-back dates read fainter against today's larger denominator — honest and intended (we genuinely held a
smaller fraction of today's universe back then).

**Binary trust** (trusted vs untrusted — nothing else). Trust is a per-FEATURE property
(`feature_trust.trust_state = 'TRUSTED'`, the binary system of record); a GROUP is trusted iff **all** its
features are. Trust is shown **in the colour** (trusted → blue, untrusted → red); raw layers are slate. Feature
groups are ordered **trusted-first**. **Raw layers** (`bars` / `trades` / `quotes`) are added as their own
columns at the FAR LEFT — the data substrate — with coverage = tickers-with-that-layer-that-date / the same
fixed universe (read from the raw manifests via `raw_coverage`); they are not trust-graded, hence slate.

**Horizontal group → feature expand.** Clicking an (expandable) feature-group column expands it INLINE into its
individual feature sub-columns, each its own column at the group's coverage (features in a group share the
(group, date) partition, so a feature's coverage equals its group's — the expand needs no extra store I/O). The
value of the expand is the feature INVENTORY per group (legible feature names); collapsible. (Per-feature
NULL/validity rates — which *would* differ within a group — are a heavier follow-up reading actual values; not
in scope.)

## Architecture

* **Builder** — `services/dashboard/store_grid.py`. `universe_size()` reads the fixed denominator (the latest
  session's in-universe `universe_membership` count). `gather_window()` does the single store-reading pass
  (one bounded evenly-sampled symbol-set read per in-window partition + the raw-manifest read + the group
  feature inventory); `build_store_grid()` rolls that up into the date × column matrix against the fixed
  universe, `build_cell_drill()` derives one (group, date) cell's per-ticker list — all in-memory from the same
  gather. Read-side only; no store schema/format/fingerprint change.
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
  "universe_size": 7318,                           // the FIXED full-universe denominator (every cell)
  "n_groups": 63,
  "n_trusted_groups": 6,
  "dates":   ["2026-06-18", "2026-06-17", ...],   // rows, newest first, WEEKDAYS only
  "columns": [                                     // raw layers first, then feature groups trusted-first
    { "key": "bars",     "label": "minute bars", "kind": "raw",   "trusted": false, "features": [] },
    { "key": "calendar", "label": "calendar",    "kind": "group", "trusted": true,  "features": ["minute_of_day_et", ...] },
    ...
  ],
  "coverage": [[byte, ...], ...],                  // rows ⟂ dates, cols ⟂ columns; 0..255 (vs universe_size)
  "column_coverage_pct": [...],                    // per-column mean coverage over its present dates
  "summary": { "n_dates": ..., "n_columns": ..., "n_groups": ..., "n_trusted_groups": ..., "n_raw": ...,
               "mean_coverage_pct": ..., "universe_size": 7318 }
}
```

Rows are **weekdays only** (weekend rows never capture; a weekday with no data still renders white — honest, it
*was* a trading day).

## React SPA (the whole dashboard)

The grid UI is a Vite + React + TypeScript SPA in `services/dashboard/frontend/`, built to static assets by
the Dockerfile's `webbuild` (node) stage and served by the dashboard FastAPI app at the **ROOT `/`** (a
`StaticFiles` mount, `html=True`). The grid IS the dashboard — there is no other page. **Light theme**: WHITE
background = zero coverage. Components:

- **`theme.ts`** — `cellColor(byte, kind, trusted)` mixes WHITE → the column's dark colour (blue trusted / red
  untrusted / slate raw) by coverage. There is no trust toggle — trust is always in the colour.
- **`CanvasHeatmap.tsx`** — a **canvas** renderer (never DOM-per-cell): dates down the rows (newest at top),
  the raw layers + feature-group columns across the top (angled name headers; raw slate, trusted blue,
  untrusted red), consuming `/api/store-grid/matrix` (gzip pass-through). All columns fit one screen; the date
  axis scrolls vertically (row-windowed). Hover → tooltip. **Clicking an expandable group column expands it
  inline into its feature sub-columns** (the primary interaction); a search highlights a group column. The
  display-column list is derived from the matrix + the expanded-group set, so the expand is pure client state.
- **`App.tsx`** — fetches the matrix + polls `/api/store-grid/meta` for the **"as of HH:MM"** staleness,
  re-pulling the matrix only when a newer build exists. The legend names the four cell kinds (none / trusted /
  untrusted / raw) and surfaces the fixed universe size. The **only** loading state is the genuine first-ever
  boot (`503 {booting}`); there is no recurring "warming".
- **`Tooltip.tsx`** — the hover readout (column, kind, date, coverage %, trust).

The earlier `/api/store-grid/cell` per-ticker drill endpoint is retained (a group cell's ticker breakdown), but
the v3 primary interaction is the horizontal feature expand.

### Stripped-down dashboard surface

This PR strips the dashboard to ONLY the grid. **Deleted** (UI-only page modules + their routes): `status_page`,
`scorecard_page`, `raw_coverage_page`, `sector_coverage_page`, `sector_coverage`, `universe_coverage_page`,
`universe_coverage`, `liquidity_bands_page`, `liquidity_bands`, `store_glimpse`, `store_glimpse_cache`, and the
old `/`, `/status`, `/jobs`, `/scorecard`, `/progress`, `/raw-coverage`, `/sector-coverage`,
`/universe-coverage`, `/liquidity-bands` HTML routes + the page-only `/api/*` routes for them. **Kept** (still
written by the host Lead loop + crons — ops continuity, not UI): `status_store.py`, `scorecard.py` +
`scorecard_store.py`, `raw_coverage.py` (a `scorecard` dependency), and `feature_grid.py` (the store
introspection the worker + scorecard reuse). The **remaining API** is the grid routes
(`/api/store-grid/*`), `/healthz`, and the ops-introspection READ routes (`/api/status/rows`,
`/api/scorecard[/history]`) backed by the live JSON stores.

## Deployment

A single clean cutover: `docker compose up -d --build mongo store-grid-worker dashboard`. The `mongo` service
comes up, the `store-grid-worker` builds the first matrix into it (confirm its log shows a matrix write), and
the dashboard serves the grid at `/`. The previous Redis-backed `store-glimpse-worker` and the old host cron
`ops/collect_store_glimpse.py` (the `1-58/3 * * * *` crontab line) are obsolete — remove that crontab line on
deploy (`crontab -e`); see the cron registry in `docs/OPERATIONS.md`.

---

## Related docs
Part of the [System Description](SYSTEM_DESCRIPTION.md) → *Dashboard & coverage grid*. See also:
[SCORECARD](SCORECARD.md) · [OBSERVABILITY](OBSERVABILITY.md) · [RAW_TAPE_COVERAGE](RAW_TAPE_COVERAGE.md) ·
[UNIVERSE_COVERAGE](UNIVERSE_COVERAGE.md) · the data it visualizes [FEATURE_PLATFORM](FEATURE_PLATFORM.md) +
[TRUST_REDESIGN](TRUST_REDESIGN.md).
