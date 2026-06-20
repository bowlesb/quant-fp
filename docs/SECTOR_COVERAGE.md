# Sector coverage surface

A read-side legibility surface — visual page AND JSON API — answering: **how much of the live trading
universe carries an FMP GICS-aligned sector label, and which sectors are thinly represented?** It makes the
recent `sector_map` unblock (and its honest *partial* coverage) visible.

Served by the dashboard FastAPI (`services/dashboard/app.py`, container `quant-dashboard-1`, host port
**8088**). Aggregation in `services/dashboard/sector_coverage.py`; page in `sector_coverage_page.py`.

## Why partial coverage is honest, not a bug

`sector_map` (populated by `scripts/populate_sector_map.py` from FMP `/profile`) holds GICS-aligned **text
labels** (`Technology`, `Financial Services`, …) the Modeller JOINs at feature-compute time. Names FMP cannot
map — mostly ETFs, warrants (`…W`/`…WS`), preferred shares (`…​.PR…`) and units (`…R`), i.e. not common
stock — get `sector = NULL` and are bucketed by the consumer as **`sector_is_unknown`**; they are **never
dropped** (see `db/init/06_sector_map.sql`). So a coverage well under 100% is correct by design. This surface
makes that split legible instead of invisible, and flags under-represented sectors (the Warehouse
"flag under-represented tickers" mission).

## Source of truth — `sector_map` ⋈ the live universe (READ-ONLY)

A read-only `SELECT` joins `sector_map` onto the **latest** `universe_membership` snapshot (the live trading
universe). No write, **no schema/format change**. Cached 60s (both inputs are slowly-changing — `sector_map`
refreshes weekly at most, the universe daily).

A symbol is **classified** only when it has a `sector_map` row with a non-blank sector. **Unknown** splits
into two honest sub-cases, kept distinct:
* **blank-sector row** — FMP returned the symbol but couldn't map a sector (`sector` NULL/empty).
* **no row at all** — the symbol isn't in `sector_map` yet.

## URLs

| URL | What |
|---|---|
| `http://<host>:8088/sector-coverage` | the visual page (HTML/JS, vanilla, fetches the JSON below) |
| `GET http://<host>:8088/api/sector-coverage` | sector-coverage JSON (`?refresh=1` bypasses the 60s cache) |

## JSON shape

```jsonc
{
  "generated_at": "2026-06-19T20:00:00+00:00",
  "universe_date": "2026-06-22",     // the universe_membership snapshot the join is over
  "universe_size": 3000,
  "n_classified": 2198,
  "n_unknown": 802,
  "classified_pct": 73.3,            // the headline coverage number (QA gate watches its inverse)
  "n_blank_sector": 496,             // unknown WITH a blank-sector sector_map row
  "n_no_row": 306,                   // unknown with NO sector_map row at all
  "n_distinct_sectors": 11,
  "sectors": [                       // classified sectors, ranked by symbol count
    {"sector": "Financial Services", "n_symbols": 594, "pct_of_universe": 19.8},
    {"sector": "Healthcare",         "n_symbols": 378, "pct_of_universe": 12.6}
    // ...
  ],
  "unclassified_sample": ["AACBR", "AACIW", "ABR.PRD", "ACHR.WS", ...],  // capped at 40, alphabetical
  "sector_map": {                    // whole-table totals, independent of the universe
    "n_rows": 4980, "n_classified": 2697, "n_distinct_sectors": 11
  }
}
```

Invariants: `n_classified + n_unknown == universe_size` and `n_blank_sector + n_no_row == n_unknown`.

## The page

* **Classified vs unknown** — a stat row (universe size / classified / unknown / classified % / sectors
  present) + a single split bar (green classified vs orange unknown), with the blank-row-vs-no-row breakdown
  and the whole-table `sector_map` totals beneath.
* **Per sector** — a ranked table (sector · symbol count · % of universe · inline proportion bar) so a
  thin sector reads off at a glance.
* **Unclassified sample** — chips of the first 40 unmapped tickers, surfacing the ETF/warrant/preferred tail.
