# Liquidity-band reference surface (canonical ADV rank)

A read-side reference surface — visual page AND JSON API — giving the system **one canonical liquidity
partition**: every symbol ranked by trailing dollar volume and cut into ADV-rank bands, with each band's
composition and membership stability.

## Why this exists

Every research lane re-derives its own liquidity cut from raw dollar volume:

- Lane C's overnight boundary adjudication used ADV-rank bands **B1-B5**;
- FeatureInventor screens on **"top-400 liquid"**;
- the deep-raw pilot pulls **"top-500 by ADV"**.

They all mean the same thing (rank by trailing dollar volume, cut into bands) but each hand-rolls the cut, so
"which band is AAPL in / how big is B4 / is the band membership stable" has no single answer. This surface
**is** that single answer, so a lane references it instead of re-deriving — and a human can see the liquidity
ladder at a glance.

It sits beside the feature-coverage grid (`docs/FEATURE_DASHBOARD.md`) and the raw-tape coverage surface
(`docs/RAW_TAPE_COVERAGE.md`). Served by the dashboard FastAPI (`services/dashboard/app.py`, container
`quant-dashboard-1`, host port **8088**). Aggregation in `services/dashboard/liquidity_bands.py`; page in
`liquidity_bands_page.py`.

## Definition (canonicalizes the EXISTING Lane-C convention)

Mirrors `experiments/2026-06-19-laneC-scope-horizon/build_bands.py` — the band hypothesis the overnight
boundary adjudication already ran on. This is the canonical **readout** of that convention, not a new one.

- **RTH dollar volume**, per (symbol, date) = `sum(close * volume)` over the regular-session minutes
  (13:30..19:59 UTC = 09:30..15:59 ET).
- **ADV (point-in-time)**, per (symbol, date) = trailing-20d rolling mean of RTH dollar volume (raw $).
- **Stable per-symbol ADV** = mean of the trailing-20d ADV over the symbol's valid days. A symbol needs
  **≥ 60 valid days** to receive a stable cross-sectional rank (so a freshly-listed name doesn't get a
  spurious rank). Symbols are ranked **descending** by stable ADV (rank 1 = most liquid).
- **Bands** (lo inclusive, hi exclusive, by ADV rank): `B1` 1-500 (most liquid), `B2` 500-1000,
  `B3` 1000-2000, `B4` 2000-4000 (small-cap), `B5` 4000-6000 (micro).

## Source — raw minute bars over a BOUNDED window (no schema change, no write)

Unlike the raw-tape surface (a pure manifest read), ADV needs the dollar-volume bodies, which are **not** in
the manifest — so this reads the raw bars (`<store>/raw/bars`). To stay snappy it reduces only the most-recent
`window_days` (default **85**) trading dates — enough for the 60-day rank floor + the 20-day ADV warmup — not
the full 18-month tape. The store is mounted **read-only**, so nothing is written; the ~25-30s cold reduction
is amortized behind a **10-minute** TTL cache (bands only move on the daily acquire). No live feature def, no
fingerprint surface, **no schema/format change**.

## Two questions it answers

- **COMPOSITION** — how many symbols sit in each band today, and their ADV range / boundaries (so "B4 = ADV
  rank 2000-4000, median ADV ~$4.7M, min ~$1.3M" reads off at a glance). Uses the **stable** per-symbol rank
  (Lane C's convention).
- **MEMBERSHIP STABILITY** — band turnover over the window using the **point-in-time** band (each date's own
  trailing-20d ADV rank): of the symbols in band B today, what fraction were in the same band N days ago. A
  high retained-fraction = a stable universe a lane can treat as fixed; low = real liquidity churn across the
  boundary. (The stable-rank band would read a trivial ~100% here by construction.)

## URLs

| URL | What |
|---|---|
| `http://<host>:8088/liquidity-bands` | the visual page (HTML/JS, vanilla, fetches the JSON below) |
| `GET http://<host>:8088/api/liquidity-bands` | the band surface: composition + stability |
| `GET http://<host>:8088/api/liquidity-bands/symbol/{symbol}` | one symbol's rank + band + ADV |
| `GET http://<host>:8088/api/liquidity-bands/members/{band}` | a band's universe, most-liquid first |

All accept `?window_days=N` (default 85; `0` = full history, slow). `/api/liquidity-bands` accepts
`?refresh=1` to bypass the 10-min TTL cache; `members` accepts `?limit=N` (default 250).

## JSON shapes

### `GET /api/liquidity-bands`

```jsonc
{
  "generated_at": "2026-06-19T...Z", "store_root": "/store", "window_days": 85,
  "anchor_date": "2026-06-18", "window_first": "2026-03-18", "window_last": "2026-06-18",
  "n_dates": 66, "n_ranked_symbols": 6093, "adv_window": 20, "min_days_for_rank": 60,
  "bands": [
    { "band": "B1", "label": "rank 1-500 (most liquid)", "rank_lo": 1, "rank_hi": 500,
      "n_symbols": 489, "adv_min": 212580374.4, "adv_median": 391912911.71, "adv_max": ... }
    // ... B2..B5
  ],
  "stability": [
    { "band": "B1", "n_today": 500, "retained_5d_pct": 96.0, "retained_20d_pct": 88.2 }
    // ... B2..B5  (point-in-time band turnover)
  ]
}
```

`adv_*` are null for a band with no members on the anchor date (null ≠ 0; a $0-ADV name would be wrong).

### `GET /api/liquidity-bands/symbol/{symbol}`

```jsonc
{ "symbol": "AAPL", "found": true, "rank": 10, "band": "B1",
  "adv": 8444061006.32, "latest_adv20": 11356547813.32, "n_valid_days": 65 }
```

`{"symbol": "NOPE", "found": false}` when the symbol is below the rank floor or absent from the window.

### `GET /api/liquidity-bands/members/{band}`

```jsonc
{ "band": "B4", "n_symbols": 1984, "shown": 250,
  "members": [ { "symbol": "MHO", "rank": 2001, "adv": 18973577.23 }, ... ] }
```

## What it is NOT

Not a feature, not a live signal, not a tradeable universe definition — a **reference readout** of the
existing ADV-band convention, for human legibility and lane reuse. The bands deliberately match the
already-adjudicated Lane-C cut; if that convention changes, update `BANDS` in `liquidity_bands.py` in
lock-step with `build_bands.py`.
