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
| `GET http://<host>:8088/api/feature-grid/thin-live-symbols` | cross-group roll-up: which SYMBOLS are backfill-only (under-represented LIVE) across the most groups (`?limit=N`) |
| `GET http://<host>:8088/api/feature-grid/timeline` | (group × recent-day × source) presence grid + per-group history-depth & live-horizon (`?days=N`) |
| `GET http://<host>:8088/api/feature-grid/orderflow-trend` | per-recent-day LIVE-stream symbol breadth across the order-flow groups — is FP_TICK_SYMBOLS coverage widening or stalling (`?days=N`) |
| `GET http://<host>:8088/api/feature-grid/trust-frontier` | TRUST FRONTIER: features split TRUSTED / ELIGIBLE (no open defect, earns trust on the next clean sweep) / BLOCKED (open parity defect) + projected trusted-% |
| `http://<host>:8088/raw-coverage` | RAW-tape coverage (one layer below this grid): per raw layer (bars/trades/quotes) DEPTH + symbols-per-day BREADTH — see `docs/RAW_TAPE_COVERAGE.md` |
| `GET http://<host>:8088/api/raw-coverage` | raw-tape coverage JSON, per layer (`?days=N`, `days=0`=full) |

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

### `GET /api/feature-grid/thin-live-symbols`

The **cross-group** inverse of the per-group surface: the `{group}/symbols` view answers "which names is THIS
group thin on"; this answers "which NAMES are under-represented LIVE across the most groups" — the
system-wide ticker-representation flag for the FP_TICK_SYMBOLS coverage gap. A symbol's `n_under_groups` is
how many LIVE groups (non-empty stream universe today) have it in backfill but not on the stream.

Under-representation is scored **only over live groups**: a group the stream never subscribes (zero stream
symbols) would otherwise mark its entire backfill universe thin and swamp the ranking, so it is recorded in
the `groups` breakdown but excluded from the per-symbol score. Symbols rank thinnest-first (most under-rep
groups, then fewest groups carrying it live, then name); `?limit=N` caps the returned list (default 50).

```jsonc
{
  "generated_at": "2026-06-18T...Z", "store_root": "/store",
  "n_live_groups": 47,              // groups with a non-empty stream universe today (the scoring base)
  "n_groups": 51,
  "n_thin_symbols": 870,            // distinct symbols under-represented in >=1 live group
  "limit": 50,
  "symbols": [
    {"symbol": "ACI", "n_under_groups": 45, "n_live_groups": 2,
     "under_groups": ["asset_flags", "calendar", "..."]}   // sorted group names
  ],
  "groups": [                       // per-group breakdown (live first, then most under-rep)
    {"group": "trade_flow", "live": true, "n_stream": 57, "n_backfill": 1268, "n_under": 1211}
  ]
}
```

### `GET /api/feature-grid/timeline`

The **time/depth** legibility view. The grid collapses every multi-day row onto a single coverage %, and the
per-group detail lists raw date arrays — neither answers, at a glance, "on each of the last N days did stream
and/or backfill land for this group, and how far back does each source's history reach". This does:

* **Presence grid** — `days` columns (most-recent first, ending at the latest store date). Each `(group, day)`
  cell carries the stream/backfill symbol counts and a `provenance` class: `both`, `stream_only` (not yet
  parity-checkable), `backfill_only` (settled, no live capture that day), `absent` (neither — e.g. a weekend).
  So live-vs-backfill provenance per `(group, day)` reads straight off the grid.
* **Depth** — per group, `backfill_earliest` + `backfill_span_days` (how far back history reaches) and
  `stream_horizon_days` (how many recent **weekdays** the live stream captured **unbroken** from the anchor,
  skipping weekends) — history depth and live horizon side by side.

`?days=N` sets the window (default 21, capped at `TIMELINE_MAX_DAYS`). Read-side only: reuses the same
one-pass per-date symbol read the grid already pays for, so it is no extra store I/O.

```jsonc
{
  "generated_at": "2026-06-18T...Z", "store_root": "/store",
  "anchor_date": "2026-06-18", "earliest_date": "2024-01-02",
  "days": 21,
  "dates": ["2026-06-18", "2026-06-17", "..."],   // most-recent first
  "groups": [
    {"group": "calendar", "version": "1.0.0", "layer": "B", "n_features": 9,
     "backfill_earliest": "2024-01-02", "backfill_latest": "2026-06-18", "backfill_span_days": 899,
     "stream_earliest": "2026-06-15", "stream_latest": "2026-06-18", "stream_horizon_days": 4,
     "days": [
       {"date": "2026-06-18", "stream": 1054, "backfill": 1268, "provenance": "both"},
       {"date": "2026-06-14", "stream": 0, "backfill": 0, "provenance": "absent"}      // weekend
     ]}
  ]
}
```

### `GET /api/feature-grid/orderflow-trend`

The **order-flow live-coverage TREND**. The timeline shows per `(group, day)` presence and the per-symbol
surfaces (`/symbols`, `thin-live-symbols`) show *which* names are thin on the **latest** day. Neither answers
the trend question for the universe-wide live order-flow certification: across the tick-derived groups, how
many **distinct symbols** did the live stream actually carry on each of the last N days, and is that union
**climbing off the ~24-canary floor or flat**?

For each recent day, over the order-flow groups present on disk (`ORDERFLOW_GROUPS`; bar/price groups are
deliberately excluded so their ~universe-wide stream coverage doesn't drown the order-flow signal):

* `n_union` — distinct symbols live on the stream in **at least one** order-flow group that day (the widest
  live order-flow universe; the headline trend number).
* `n_intersection` — symbols live in **every** order-flow group that captured anything that day (the
  full-coverage core; absent groups don't zero it out).
* `per_group` — each group's live stream symbol count, so a single thin group is visible against the union.

The header carries `union_delta` = newest-captured-day `n_union` − oldest-captured-day `n_union` (> 0
widening, 0 flat, < 0 shrinking). `?days=N` sets the window (default 21, capped at `TIMELINE_MAX_DAYS`).
Read-side only: reads `_read_symbols` (bounded per-partition sampling) over **only** the recent window per
order-flow group — no extra heavy I/O beyond what the grid already does.

```jsonc
{
  "generated_at": "2026-06-18T...Z", "store_root": "/store",
  "anchor_date": "2026-06-18", "days": 21,
  "groups": ["inter_arrival", "liquidity", "...", "trade_flow"],   // order-flow groups present on disk
  "dates": ["2026-06-18", "2026-06-17", "..."],                    // most-recent first
  "newest_captured_union": 1054, "oldest_captured_union": 428, "union_delta": 626,  // WIDENING
  "trend": [
    {"date": "2026-06-18", "n_union": 1054, "n_intersection": 11, "n_live_groups": 7,
     "per_group": {"trade_flow": 1054, "signed_trade_ratio": 803, "...": 0}}
  ]
}
```

### `GET /api/feature-grid/trust-frontier`

The **trust FRONTIER** — how close the feature set is to fully trusted, and *why* the not-yet-trusted ones
aren't. The grid badge shows each feature's flat lifecycle grade; what it cannot show is that a feature whose
parity defect has been **cleared** is one clean sweep from TRUSTED (its DIVERGENT badge stays red until the
next sweep re-grades it). This view joins the binary-trust set (`feature_trust.trust_state = 'TRUSTED'` — the
consumable predicate downstream agents gate on) against the **OPEN** rows of `feature_parity_defect`
(read-only, no new source of truth), scoped to the current registry catalog, and splits every feature into:

* `TRUSTED` — has earned binary trust.
* `ELIGIBLE` — not yet trusted **and no open parity defect**: accruing toward trust, advances to TRUSTED on
  the next clean settled sweep. This is the frontier the flat badge hides (a defect-cleared DIVERGENT lands
  here, not in permanent red).
* `BLOCKED` — still has an open parity defect; needs a fix, does **not** advance on the next sweep (today this
  is the FP_TICK_SYMBOLS tick-coverage tail — `trade_flow` / `quote_spread` lead the blocked count).

`projected_trusted_pct` = `(trusted + eligible) / total` — where trust lands if every eligible feature passes
the next clean sweep, i.e. the headline of the coming jump *before* it happens. Per-group rows carry the same
split (ranked most-blocked-first) plus the `blocked_features` names, so the genuinely-stuck families surface
on top. No `?days` (it's a point-in-time snapshot); `?refresh=1` bypasses the TTL cache.

```jsonc
{
  "generated_at": "2026-06-19T...Z",
  "n_features": 682, "n_trusted": 106, "n_eligible": 520, "n_blocked": 56, "n_open_defects": 56,
  "trusted_pct": 15.5, "eligible_pct": 76.2, "blocked_pct": 8.2,
  "projected_trusted_pct": 91.8,   // if every ELIGIBLE feature passes the next clean sweep
  "groups": [                       // most-blocked-first
    {"group": "trade_flow", "layer": "C", "n_features": 28,
     "n_trusted": 0, "n_eligible": 5, "n_blocked": 23,
     "trusted_pct": 0.0, "projected_trusted_pct": 17.9,
     "blocked_features": ["trade_flow_buy_ratio_5m", "..."]}
  ]
}
```

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
