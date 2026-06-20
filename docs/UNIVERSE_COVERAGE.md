# Universe coverage surface

A read-side legibility surface — visual page AND JSON API — answering: **how much of the AVAILABLE filtered
universe is actually being captured, per session, over time?** It turns the whole-universe captured-vs-available
ratio into a permanent dashboard fixture, so a silent live-capture re-cap (like the 06-16 relaunch's default
`UNIVERSE_MAX_SYMBOLS=3000`-of-~7.3k cap) is instantly visible rather than a one-time catch.

Served by the dashboard FastAPI (`services/dashboard/app.py`, container `quant-dashboard-1`, host port
**8088**). Aggregation in `services/dashboard/universe_coverage.py`; page in `universe_coverage_page.py`.

## What it complements

The [#223 coverage-DROP detector](FEATURE_DASHBOARD.md) flags a per-**group** thinning relative to that group's
own in-window peak (a group losing live symbols). This is its **whole-universe complement**: per day, how many
symbols are in the **captured** universe vs how many **could** be — the single ratio that exposes a universe-wide
capture cap. The 06-16 → 06-22 step from 11336 to a flat 3000 reads off immediately as a `capped` status.

## Captured vs available — the two sides (READ-ONLY)

* **Captured** = `universe_membership.in_universe` count for a `trade_date` — the symbol set the seed actually
  put into the live capture session (`quantlib.features.seed_universe`). Genuinely per-day.
* **Available** = every tradable, primary-venue, non-ETF-like, non-slash symbol in `asset_metadata` — the EXACT
  screen `seed_universe.select_universe` applies (`KEEP_EXCHANGES` + `is_etf_like`) **minus the `MAX_SYMBOLS`
  cap**. So it reproduces the full set the seed would cap from. `asset_metadata` is refreshed each seed run with
  no per-day history, so this is a **snapshot** used as the SAME denominator for every day.

The ETF/fund screen is the pure Python `quantlib.universe.is_etf_like` regex, so the available count is computed
in Python here (not SQL) — byte-for-byte the seed logic, guaranteeing the count matches what the seed produces.

A day captured **above** the current available set (e.g. 06-15's 11336, seeded before the `is_etf_like` screen
was added) is **flagged** (`over_available`) and its ratio clamped to 100% — informational only, not hidden.

This module only `SELECT`s from `universe_membership` + `asset_metadata` and applies the existing pure screen —
**NO schema/format change, no write, NO store I/O**. Cached 60s (both inputs are daily/per-seed). The two DB
reads are isolated into helpers so tests monkeypatch them without a DB.

## URLs

| URL | What |
|---|---|
| `http://<host>:8088/universe-coverage` | the visual page (HTML/JS, vanilla, fetches the JSON below) |
| `GET http://<host>:8088/api/universe-coverage` | universe-coverage JSON (`?days=N` window, `?refresh=1` bypasses the 60s cache) |

## Status bands (the headline)

| status | captured / available | meaning |
|---|---|---|
| `full` | ≥ 90% | effectively full breadth |
| `thinned` | 60–90% | partial — some breadth left on the table |
| `capped` | < 60% | a hard cap / regression (the 3000-of-7349 = 40.8% state) |

## JSON shape

```jsonc
{
  "generated_at": "2026-06-20T...Z",
  "available": 7349,                 // filtered set size (same denominator every day)
  "status": "capped",                // = latest day's status
  "ratio_thresholds": {"ok": 0.90, "thin": 0.60},
  "latest": {                        // newest captured session
    "date": "2026-06-22",
    "captured": 3000,
    "ratio": 0.4082, "ratio_pct": 40.8,
    "uncaptured": 4349,              // available - captured (names left on the table)
    "status": "capped",
    "over_available": false          // true => pre-screen seed, ratio clamped to 100%
  },
  "timeline": [ /* same per-day shape, newest first, clipped to ?days */ ]
}
```
