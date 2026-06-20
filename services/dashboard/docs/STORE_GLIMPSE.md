# Feature-store glimpse — the live store-at-a-glance grid

`GET /store-glimpse` (HTML) · `GET /api/store-glimpse` · `GET /api/store-glimpse/{group}/tickers`

The **immediate glimpse into our current features**: at one look, *what features exist, how covered, how
fresh, and their trust* — the live state of the feature store. A new panel in the coverage-dashboard
lineage, extending the #221 coverage heat / #223 drop detector / #227 universe coverage (it reuses their
data + patterns, it does not duplicate them).

## The grid

- **Rows = DATES**, most-recent at TOP (Today, Yesterday, … back `days`, default 30, max 90). Each row is a
  captured session.
- **Columns = FEATURE GROUPS** (the ~63 registry groups), each **expandable on click** to its individual
  features (F1 | F2 | F3 …), plus a **Total** summary column.
- **Each (date × group-or-feature) cell** is a tiny box with **two independent visual encodings** (the two
  annotations on Ben's sketch):
  1. **DARKNESS / opacity = coverage** — the fraction of the captured universe (default 7318, the #227
     available filtered set, env-overridable via `GLIMPSE_UNIVERSE_SIZE`) that has this group on this date:
     `coverage = n_symbols_that_day / universe_size`. Darker = more tickers covered; **absent = blank**.
     This is the #221 coverage-VOLUME heat, but normalized to the **whole universe** (not the group's own
     peak) so a thin order-flow group reads honestly thin against a full-universe bar group.
  2. **COLOR / hue = trust** — green = trusted (`VALIDATED`), amber = pending, red = divergent, grey =
     ungraded, from the `feature_trust` table (the same source #221/#223 read). A group cell takes the
     worst-actionable hue of its features (divergent first, then trusted, then pending, then ungraded); a
     feature cell takes its own feature's hue. So a cell shows **coverage (darkness) and trust (color)
     together**.

Per-feature coverage equals its group's coverage (features in a group are co-captured in the same
`(group, date)` partition), so feature-expansion is free of extra store I/O — only the hue differs per
feature.

## Drill-down — ticker × date

Clicking a `(date × group)` cell opens a **TICKER × DATE** grid for that group (Ben's "one box per ticker
and date"): one row per ticker, one box per date, shaded by provenance — `both` / `stream` / `backfill` /
`absent`. **Lazy** (only fetched on a cell click) and **paginated** (`limit` rows, default 500, ranked
most-covered first; the universe is ~7.3k). Served by `GET /api/store-glimpse/{group}/tickers`.

## Live refresh

The page **auto-refreshes every 30s** so it always reflects the current store. The refresh is cheap: a
60s TTL cache (`StoreGlimpseCache`) makes a repeat fetch instant (a cached hit returns in microseconds).

## Performance — windowed read

Unlike the #221 grid (which reads every group's whole multi-year backfill history), the glimpse is
**windowed**: it finds the store anchor from directory names (no parquet read), then reads symbol *counts*
only for the dates **in the grid window** — so a 30-row grid pays ≤30 dates/source/group, not the full
history. The per-partition reads reuse `feature_grid`'s bounded evenly-spaced file sampling (a 7k-file
stream partition is ~12 reads, not 7k). A cold build is a once-per-TTL cost; every live auto-refresh in
between is a cached, microsecond hit.

Read-side only. No schema/format/fingerprint change. No new third-party import — the dashboard import
closure (guarded by the #234 static dep-closure test) is unchanged.

## JSON shape

```json
{
  "generated_at": "2026-06-20T…Z",
  "store_root": "/store",
  "anchor_date": "2026-06-18",
  "days": 30,
  "universe_size": 7318,
  "summary": {
    "n_groups": 63, "n_features": 728, "n_dates": 30,
    "n_trusted": 0, "trusted_pct": 0.0,
    "trust_counts": {"trusted": 0, "pending": 109, "divergent": 519, "ungraded": 100}
  },
  "groups": [
    {"group": "breadth", "version": "…", "n_features": 6, "trust_hue": "divergent",
     "trust_counts": {"trusted": 0, "pending": 1, "divergent": 5, "ungraded": 0},
     "features": [{"feature": "…", "trust_hue": "divergent", "lifecycle_state": "DIVERGENT"}, …]}
  ],
  "dates": ["2026-06-18", "2026-06-17", …],
  "cells": {
    "2026-06-18": {
      "breadth": {"coverage": 0.385, "n_symbols": 2820, "hue": "divergent"},
      "__total__": {"coverage": 0.385, "n_symbols": 2820, "hue": "divergent"}
    }
  }
}
```

Drill (`/api/store-glimpse/{group}/tickers`):

```json
{
  "group": "breadth", "anchor_date": "2026-06-18", "days": 30,
  "n_tickers": 10523, "limit": 500, "dates": ["2026-06-18", …],
  "tickers": [{"symbol": "A", "n_present": 4,
               "boxes": [{"date": "2026-06-18", "provenance": "both"}, …]}]
}
```
