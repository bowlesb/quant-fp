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

## Deployment

The `store-glimpse-worker` service **replaces the host cron** `ops/collect_store_glimpse.py` (the
`1-58/3 * * * *` crontab line). On deploy: bring up the worker (`docker compose up -d store-glimpse-worker`),
confirm its log shows a first matrix write, then remove that crontab line (`crontab -e`) — see the cron
registry in `docs/OPERATIONS.md`. The old group×date `/store-glimpse` page is removed in a follow-up PR (the
React SPA grid); this PR ships the worker + API + the kill of the recurring "warming" path.
