# Store grid — ticker × date coverage matrix (always-warm)

The **immediate glimpse into the feature store**: one fast, always-warm grid whose **rows are DATES** (most
recent at top, ~18 months back) and **columns are TICKERS** (the captured universe, ~11k including delisted
names over the window). Each **cell** encodes, as darkness, the **proportion of the feature store present for
that ticker on that date** — the HEIC tiny-boxes view. A binary trust overlay marks cells whose every
covering feature-group is fully trusted.

This replaces the old server-rendered group×date `/store-glimpse` page (a 38–50s on-request store scan behind
a host cron + 1h Redis TTL that surfaced a "warming…" placeholder on any cold/expired hit). The matrix is now
built by a **permanent background worker** and served from a last-good Redis blob — the only loading state a
reader ever sees is the genuine first-ever boot.

## What a cell means

```
coverage = (# feature-GROUPS present for this ticker on this date) / N_REGISTRY_GROUPS
```

The denominator is the **total** registry group count, not "groups that have any data that date", so the
18-month depth gradient reads truthfully: far-back dates (where only the calendar groups backfill) read
**faint**; recent fully-captured days read **dark**. Coverage is quantized to a byte (0..255) for a compact
packed matrix. Store depth is genuinely uneven — only the calendar groups go back ~18 months, most groups are
shallow (recent weeks/months) — so the faint far-back rows are **honest sparsity, not a bug**.

**Binary trust** (trusted vs untrusted — nothing else). Trust is a per-FEATURE property
(`feature_trust.trust_state = 'TRUSTED'`, the binary system of record). Projected onto a cell as one bit: a
cell is "all-trusted" iff **every** group present for that ticker×date is fully trusted (all its features
trusted); otherwise untrusted. No PENDING / DIVERGENT / UNGRADED states on this view.

## Architecture

* **Builder** — `services/dashboard/store_grid.py`. `gather_window()` does the single store-reading pass
  (one bounded evenly-sampled symbol-set read per in-window partition, reusing `feature_grid._read_symbols`);
  `build_store_grid()` assembles the packed matrix from it, `build_ticker_drill()` derives one ticker's
  per-(date×group) presence — both purely in-memory from the same gather, so pre-warming N drills costs no
  extra store I/O. Read-side only; no store schema/format/fingerprint change.
* **Worker** — `services/dashboard/store_grid_cache.py`, run as the **`store-glimpse-worker`** compose
  service (`restart: unless-stopped`, built from the dashboard image, mounts `fp_store_real:/store:ro`, on
  `quant_default` to reach `quant-redis`). A permanent loop: build on boot, write a gzip-compressed matrix
  blob + the top-N ticker drills + a meta header to Redis, refresh a 24h TTL, sleep ~180s, repeat. A full
  rebuild measures ~3–4 min (dominated by the deep calendar groups' many partitions); the data changes slowly
  (capture per-session, trust per-sweep), so this cadence is plenty fresh and the last-good blob always
  serves between builds.
* **Reader** — `services/dashboard/app.py` routes below. A sub-ms Redis GET; the matrix route passes the
  stored gzip bytes straight through with `Content-Encoding: gzip` (the dense ~2.8M-cell matrix is ~38 MB raw
  JSON, ~130 KB gzipped — no build, no recompress on the request path).

## URLs

| URL | What |
|---|---|
| `GET /api/store-grid/matrix` | the packed ticker×date coverage + binary-trust matrix (gzip; the React grid's feed). `503 {booting}` only before the worker's first write. |
| `GET /api/store-grid/meta` | small header: `generated_at`, `anchor_date`, dims, gzip size, build seconds — the UI's "as of HH:MM:SS" staleness. |
| `GET /api/store-grid/ticker/{symbol}` | one ticker's per-(date×group) presence + per-group binary trust (the cell-click drill). |

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
  "tickers": ["AAPL", ...],                        // columns, default-sorted most-covered first
  "coverage": [[byte, ...], ...],                  // rows ⟂ dates, cols ⟂ tickers; 0..255
  "trusted":  [[bit, ...], ...],                   // 1 = every present group fully-trusted
  "coverage_pct": [...],                           // per-ticker mean coverage over present dates (sort key)
  "legend": { "coverage_scale": "...", "trust_overlay": "...", "depth_note": "..." },
  "summary": { "n_dates": ..., "n_tickers": ..., "n_groups": ..., "n_trusted_groups": ..., "mean_coverage_pct": ... }
}
```

Rows are **weekdays only** (weekend rows never capture, so dropping them keeps the matrix ~30% tighter; a
weekday with no data still renders blank — honest, it *was* a trading day).

## Drill-down visual nesting (PR 2 — React UI requirement)

When the user clicks a cell/column and it **expands** to show detail (the per-ticker × per-group drill, or
any future expansion), it must be **visually unmistakable that the expanded content belongs to the thing that
was clicked** — the current grid is ambiguous about this and Ben flagged it directly. Every expand/drill, at
every level, must make the parent→child relationship obvious by combining:

- a **labeled header / tab** on the expanded panel naming its parent and giving the count
  (e.g. `swing · 2026-06-18 — 412 tickers`, or `AAPL — 63 groups`);
- **indentation / a contained card** so the child rows visibly sit *inside* the parent;
- a **distinct background shade** for the expanded region (lighter or darker than the grid) so the boundary
  is unmistakable;
- **tighter / slightly smaller child rows** so they read as detail under the summary.

Keep this treatment **consistent across all drill levels**. Implemented in `DrillPanel.tsx`: the panel header
is a chip naming the parent ticker + group/trust counts; each group row is a tighter child row with a binary
trust pill; expanding a group reveals a further-indented, distinctly-shaded nested card whose own header chip
names *its* parent (the group) — the same treatment one level down.

## React SPA (PR 2)

The grid UI is a Vite + React + TypeScript SPA in `services/dashboard/frontend/`, built to static assets by
the Dockerfile's `webbuild` (node) stage and served by the dashboard FastAPI app at **`/store-grid`** (a
`StaticFiles` mount, `html=True`). Components:

- **`CanvasHeatmap.tsx`** — a **canvas** renderer (never DOM-per-cell): dates down the rows (newest at top),
  tickers across the columns, cell darkness = coverage, consuming `/api/store-grid/matrix` (gzip pass-through).
  Only the visible column/row window is painted each frame (horizontal virtualization), so it stays smooth at
  392 × 11.4k. Hover → tooltip; click a column → the drill panel; a search jump scrolls + highlights a column.
- **`App.tsx`** — fetches the matrix + polls `/api/store-grid/meta` for the **"as of HH:MM:SS"** staleness,
  re-pulling the matrix only when a newer build exists. The binary-trust overlay is a single toggle (trusted
  green / untrusted grey — no other states). The **only** loading state is the genuine first-ever boot (the
  API's `503 {booting}`); there is no recurring "warming".
- **`Tooltip.tsx`** / **`DrillPanel.tsx`** — the hover readout and the nested drill described above.

This SPA **replaces both** old server-rendered pages: `/feature-grid` and `/store-glimpse` (and their page
modules `feature_grid_page.py` / `store_glimpse_page.py`) are deleted. The underlying `feature_grid.py`
store-introspection logic (reused by the worker, `store_glimpse.py`, and `scorecard.py`) and the
`/api/feature-grid/*` + `/api/store-glimpse/*` JSON routes are kept.

## Deployment

The `store-glimpse-worker` service **replaces the host cron** `ops/collect_store_glimpse.py` (the
`1-58/3 * * * *` crontab line). On deploy: rebuild + bring up the dashboard and the worker
(`docker compose up -d --build dashboard store-glimpse-worker`), confirm the worker log shows a first matrix
write, open `/store-grid` to confirm the React grid serves, then remove that crontab line (`crontab -e`) — see
the cron registry in `docs/OPERATIONS.md`.
