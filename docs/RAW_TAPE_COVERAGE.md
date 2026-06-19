# Raw-tape coverage surface

A read-side legibility surface — visual page AND JSON API — answering, for every RAW Alpaca layer we
acquire (minute **bars**, tick **trades**, tick **quotes**): **how much raw history is on disk, and how
broadly?** This is the substrate the modellers invent + backfill features on *without re-downloading*, so
seeing it at a glance is the precondition for the feature-invention loop and the deep-raw-history priority.

It sits one layer BELOW the feature-coverage grid (`docs/FEATURE_DASHBOARD.md`): that grid is per FEATURE
group; this is per RAW tape. As the deep backfill fills, this same surface is the live progress tracker
(quotes depth climbing, trades breadth widening).

Served by the dashboard FastAPI (`services/dashboard/app.py`, container `quant-dashboard-1`, host port
**8088**). Aggregation in `services/dashboard/raw_coverage.py`; page in `raw_coverage_page.py`.

## Source of truth — the raw manifests, NOT a store scan

`quantlib.data.raw_store` records one `(tier, symbol, date, rows, bytes)` cell per acquired symbol-day in
`<store>/raw/_manifest_<tier>.d/` (append-only parts + a legacy single file). That manifest **is** the
authoritative coverage record, so this is a cheap read (~4s cold for the full store, dominated by part
count; cached 60s) — no partition-tree walk, no parquet bodies read, **no schema change**.

A manifest cell with `rows == 0` is a settled-empty / not-yet-settled marker (an illiquid-delisted
symbol-day, or a recent fetch before the tape landed — see `raw_store.resumable_done_keys`), **not** a real
tape. Everything here is computed over REAL cells (`rows > 0`): a day "has" a layer only where a real tape
landed, and symbols-per-day counts only symbols with a real tape that day — the honest "what can I invent
on".

## URLs

| URL | What |
|---|---|
| `http://<host>:8088/raw-coverage` | the visual surface (HTML/JS, vanilla, fetches the JSON below) |
| `GET http://<host>:8088/api/raw-coverage` | raw-tape coverage JSON, per layer (`?days=N`, `days=0`=full) |

`?refresh=1` bypasses the 60s TTL cache. `?days=N` clips each layer's per-date timeline to the most-recent N
calendar days (default 90; `days=0` = full history). Summary depth/breadth stats are **always** over the
full tape — the window only trims the timeline arrays.

## What it shows, per layer

* **DEPTH** — `earliest` / `latest` date, `span_days` (calendar span), `n_dates` (dates with a real tape).
  The date-coverage strip on the page renders one tick per trading weekday in the window, so **gaps** (a
  weekday with no tape) read off at a glance.
* **BREADTH** — distinct `n_symbols` overall, `mean` / `median` / `newest_symbols_per_day`, and a per-date
  `dates: [{date, n_symbols, rows}]` timeline rendered as a symbols-per-day bar chart. Makes the standing
  gaps obvious: trades thin (~1.9k sym/day vs bars ~6.3k), quotes shallow (only ~3 months).

## JSON shape

```
{generated_at, store_root, days, anchor_date, span_earliest, span_latest,
 layers: [{tier, label, earliest, latest, span_days, n_dates, n_symbols, n_cells,
           mean_symbols_per_day, median_symbols_per_day, newest_symbols_per_day,
           shown_from, n_dates_shown, dates: [{date, n_symbols, rows}]}]}
```

A layer never acquired (e.g. a tier with no manifest) is reported as a present-but-empty layer
(`earliest: null`, `dates: []`), not an error.

## Verified truth (2026-06-18 store)

| layer | span | dates | distinct sym | mean sym/day | note |
|---|---|---|---|---|---|
| bars | 2024-12-11 → 2026-06-18 | 380 | ~7.4k | ~6.3k | deep + broad (good) |
| trades | 2024-12-12 → 2026-06-18 | 379 | ~7.4k | ~1.9k | **thin breadth** (deep-backfill target #2) |
| quotes | 2026-03-18 → 2026-06-18 | 65 | ~4.3k | ~3.8k | **shallow depth, ~3mo** (deep-backfill target #1) |

(Distinct-symbol counts are over real-tape cells, so they read slightly under the raw manifest cell count,
which includes 0-row settled-empty names.)
