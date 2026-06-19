# Feature-data coverage + trust dashboard

A visual grid (for the human) AND a JSON API (for agents) that answer, in one place: **for every feature
group, over each time period — how much DATA do we have, and is it TRUSTABLE yet?** Two orthogonal
dimensions, never conflated: a feature can be 100%-populated yet UNGRADED.

Served by the existing dashboard FastAPI (`services/dashboard/app.py`, container `quant-dashboard-1`, host
port **8088**). It reuses the existing introspection — `quantlib/features/feature_data.py`,
`quantlib/features/store.py`, the `feature_trust` trust state machine, and `REGISTRY.catalog()` — and does
not re-encode any of them. The aggregation lives in `services/dashboard/feature_grid.py`; the page in
`services/dashboard/feature_grid_page.py`.

## URLs

| URL | What |
|---|---|
| `http://<host>:8088/feature-grid` | the visual grid (HTML/JS, vanilla, fetches the JSON below) |
| `GET http://<host>:8088/api/feature-grid` | full grid JSON (groups × periods) |
| `GET http://<host>:8088/api/feature-grid/{group}` | per-feature detail for one group |
| `GET http://<host>:8088/api/feature-grid/{group}/symbols` | per-SYMBOL coverage: which tickers are live (stream) vs backfill-only (under-represented LIVE) |

All API endpoints accept `?refresh=1` to bypass the 60s TTL cache and re-aggregate. A cold build over the
live store is ~4s; cached responses are ~1ms. There is a **↻ refresh** button on the page.

## The grid

* **Rows = time periods**, top→down short to long: `1d, 1w, 1m, 2m, 6m, 12m, all`. Each period ends at the
  latest store date and looks back N calendar days (clamped to the earliest captured date so a long period
  over a short store never invents pre-history).
* **Columns = feature groups** (the ~40 registered groups).
* **Cell = (group × period)**.

### Encodings (two separate channels)

1. **Data coverage** → blue fill **opacity** (transparent = 0%, solid = 100%) **+ the % number** (always
   white with a text outline so it is legible at any opacity).
   Coverage % = `symbol-days present / (n_trading_days_in_period × peak_universe)`, where `peak_universe`
   is the group's own peak distinct-symbol count on any single date in the window (honest, no hardcoded
   universe). `n_trading_days` is a local Mon–Fri weekday count (the dashboard never calls the network /
   Alpaca calendar), clamped to the days the store actually spans.
2. **Trust state** → a distinct channel: left **border colour** + corner **badge**.
   `UNGRADED` grey · `PENDING`/validating amber (with "X% to trusted" = clean_days / days_needed) ·
   `VALIDATED` green (with "trusted Y%" = fraction of the group's features validated) · `DIVERGENT` red.
   Sourced from `feature_trust.lifecycle_state` (the contamination-aware grade) — never re-derived.
3. **Stream vs backfill** → a split-corner indicator per cell (upper bar = stream present, lower =
   backfill present). A feature is only parity-checkable (and thus trustable) when **both** sides exist.
   The cell number is the combined % (max of the two); a **show:** toggle switches it to stream-only or
   backfill-only.

Palette is colour-blind-safe (blue fill + amber/green/grey/red borders, reinforced by text labels, never
colour alone).

### Summary header + controls

The header shows totals — e.g. "0/633 features trusted · 64% mean coverage · 0 groups fully validated" —
and a legend explaining the opacity + trust encodings. Controls: **search** a feature by name, **filter**
by trust state, **sort** groups by name / coverage / trust, and the show-metric toggle.

### Expanded view

Click a cell or column header → the group's **individual features** appear as a detail table with the
trust trajectory: per feature the **trust state**, **clean-days / needed** progress bar (why it isn't
trusted yet), **clean match-rate**, **last validated**, layer, and the **description on hover**. The
group header surfaces the stream/backfill date spans and the **gap dates** (stream-only dates have no
backfill, so they are not parity-checkable — the concrete reason trust is blocked).

## JSON shapes

### `GET /api/feature-grid`

```jsonc
{
  "generated_at": "2026-06-16T19:00:00+00:00",
  "store_root": "/store",
  "anchor_date": "2026-06-16",      // latest date with any partition (every period ends here)
  "earliest_date": "2026-06-15",    // earliest captured date (floor for the "all" period)
  "periods": [ {"key": "1d", "label": "Last day", "lookback_days": 1}, ... ],
  "groups": [ {"group": "trade_flow", "version": "1.0.0", "layer": "B", "n_features": 23}, ... ],
  "cells": [
    {
      "group": "trade_flow", "period": "all",
      "coverage_pct": 72.0,         // combined = max(stream, backfill); drives the blue opacity + number
      "stream_pct": 72.0,
      "backfill_pct": 50.0,
      "n_features": 23,
      "n_symbols": 1989,            // peak distinct symbols in the window
      "n_dates": 2,                 // dates with a partition in the window
      "trust_state": "UNGRADED",    // UNGRADED | PENDING | VALIDATED | DIVERGENT
      "trust_pct": 0.0,             // % of the group's features VALIDATED
      "n_trusted": 0, "n_validating": 0, "n_ungraded": 23
    }
    // ... one cell per (group × period)
  ],
  "summary": {
    "n_groups": 40, "n_features": 633, "n_trusted": 0, "trusted_pct": 0.0,
    "mean_coverage_pct": 64.1,      // mean of the "all"-period coverage across groups
    "fully_validated_groups": 0,
    "days_needed_for_trust": 2      // clean days needed to promote PENDING -> VALIDATED
  }
}
```

### `GET /api/feature-grid/{group}`

```jsonc
{
  "group": "trade_flow", "version": "1.0.0", "n_features": 23,
  "stream_dates": ["2026-06-15", "2026-06-16"],
  "backfill_dates": ["2026-06-15", "2026-06-16"],
  "stream_first": "2026-06-15", "stream_last": "2026-06-16",
  "backfill_first": "2026-06-15", "backfill_last": "2026-06-16",
  "stream_only_dates": [],          // stream present but backfill missing -> NOT parity-checkable
  "backfill_only_dates": [],
  "features": [
    {
      "feature": "signed_volume_1m",
      "description": "Buy-minus-sell signed share volume over the last minute (tick-rule signed).",
      "layer": "B", "parity_method": "tolerance",
      "trust_state": "UNGRADED",
      "clean_days": 0, "days_needed": 2,
      "progress_to_trusted_pct": 0.0,   // min(clean_days, needed)/needed, or 100 when VALIDATED
      "clean_value_rate": null,         // lifetime clean parity match rate (null until graded)
      "last_validated_day": "2026-06-16"
    }
    // ... one row per feature in the group
  ]
}
```

Returns `404` for an unknown group.

### `GET /api/feature-grid/{group}/symbols`

Per-SYMBOL coverage for one group — the **ticker-representation** surface. The grid shows a single peak
symbol *count* per group, which hides *which* names are thin LIVE. The live stream subscribes a far smaller
universe than backfill agg covers, so an order-flow group can read ~1300 backfill symbols yet only ~50 on the
live tick stream. `backfill_only` is exactly the set under-represented LIVE. Each source is compared on its
OWN latest store date (stream and backfill backfill at different cadences).

```jsonc
{
  "group": "trade_flow", "version": "1.0.0",
  "stream_date": "2026-06-18",       // each source's own latest partition date
  "backfill_date": "2026-06-18",
  "n_stream": 57,                    // distinct symbols captured on the live stream
  "n_backfill": 1268,               // distinct symbols in the backfill agg
  "n_both": 57,
  "n_backfill_only": 1211,          // in backfill but NOT live -> under-represented LIVE (the headline)
  "n_stream_only": 0,               // live but absent from today's backfill
  "stream_coverage_pct": 4.5,       // n_stream / |stream ∪ backfill|
  "both": ["AAPL", "..."],
  "backfill_only": ["AABB", "..."], // sorted; the under-represented tickers
  "stream_only": []
}
```

Returns `404` for an unknown group.

## Deployment notes

* The dashboard image (`services/dashboard/Dockerfile`) is built from the **repo root** so it can `COPY`
  the shared `quantlib` package and `rust/` source; it imports the live registry + store introspection,
  which needs the compiled `quant_tick` kernel (same multi-stage Rust+deps recipe as
  `docker/fp-dev.Dockerfile`). Rebuilding this container has **no feature-compute fingerprint risk** — it
  never computes or serves features.
* The `fp_store_real` volume is mounted **read-only** at `/store` (`STORE_ROOT`). The trust DB is the same
  TimescaleDB the rest of the dashboard already reads.
* Sparse-data honesty: with only one or two captured days the grid shows ~100% for `1d` and dilutes
  correctly for longer periods, and trust shows UNGRADED until the nightly sweep grades features — which
  is the correct, honest picture.
