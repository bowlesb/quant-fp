"""SECTOR coverage aggregation for the dashboard — how much of the live universe is sector-classified.

``sector_map`` (FMP GICS-aligned text labels, populated by ``scripts/populate_sector_map.py``) was just
wired up. It is a slowly-changing per-symbol reference the Modeller JOINs at feature-compute time; names FMP
cannot map get ``sector = NULL`` and are bucketed by the consumer as ``sector_is_unknown`` (never dropped —
see ``db/init/06_sector_map.sql``). That partial coverage is honest BY DESIGN (the unmapped tail is mostly
ETFs / warrants / preferred / units, not common stock), but it is invisible: nothing on the dashboard shows
how much of the live universe actually carries a sector label, nor which sectors are thinly represented.

This module makes it legible. It reads ``sector_map`` joined onto the latest ``universe_membership`` snapshot
and reports, per the LIVE universe:
  * per-sector symbol COUNT (the 11 FMP-GICS sectors, ranked),
  * the CLASSIFIED-vs-UNKNOWN split (unknown = a blank/NULL-sector row OR no ``sector_map`` row at all),
  * the CLASSIFIED % of the live universe (the headline coverage number — the QA gate watches its inverse),
  * a small SAMPLE of unclassified tickers (so the mostly-ETF/warrant/preferred tail is visible at a glance).

It also reports the WHOLE ``sector_map`` table (independent of the universe) so the catalog size vs the live
universe is comparable.

READ-ONLY. It only ``SELECT``s from ``sector_map`` and ``universe_membership`` — NO schema/format change, no
write. The two DB reads are isolated into helper functions so tests can monkeypatch them without a DB.
"""

from __future__ import annotations

import datetime as dt
import os
import time

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# How many unclassified tickers to surface in the sample (alphabetical) — enough to make the
# ETF/warrant/preferred character of the tail obvious without dumping all ~800.
UNCLASSIFIED_SAMPLE = 40

# The latest universe membership snapshot + its sector join. A symbol is CLASSIFIED only when it has a
# sector_map row with a non-blank sector; a blank-sector row and a missing row are both UNKNOWN (the
# consumer's sector_is_unknown bucket), distinguished so the "FMP couldn't map" vs "not in the map yet"
# split is legible.
_UNIVERSE_SECTOR_QUERY = """
WITH latest AS (
    SELECT max(trade_date) AS d FROM universe_membership WHERE in_universe
),
universe AS (
    SELECT u.symbol
    FROM universe_membership u, latest
    WHERE u.trade_date = latest.d AND u.in_universe
)
SELECT
    universe.symbol,
    s.symbol IS NOT NULL                          AS has_row,
    s.sector IS NOT NULL AND s.sector <> ''       AS classified,
    s.sector                                       AS sector
FROM universe
LEFT JOIN sector_map s ON s.symbol = universe.symbol
"""

_LATEST_UNIVERSE_DATE_QUERY = "SELECT max(trade_date) FROM universe_membership WHERE in_universe"

_SECTOR_MAP_TOTALS_QUERY = """
SELECT
    count(*)                                              AS n_rows,
    count(*) FILTER (WHERE sector IS NOT NULL AND sector <> '') AS n_classified,
    count(DISTINCT sector) FILTER (WHERE sector IS NOT NULL AND sector <> '') AS n_distinct_sectors
FROM sector_map
"""


def _read_universe_sectors() -> list[dict[str, object]]:
    """Each live-universe symbol with its sector-classification flags. Isolated so tests monkeypatch it."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_LATEST_UNIVERSE_DATE_QUERY)
        row = cur.fetchone()
        latest_date = row[0] if row else None
        cur.execute(_UNIVERSE_SECTOR_QUERY)
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, record)) for record in cur.fetchall()]
    # Attach the snapshot date to every row so the pure builder has it without a second read path.
    for record in rows:
        record["_universe_date"] = latest_date.isoformat() if latest_date is not None else None
    return rows


def _read_sector_map_totals() -> dict[str, object]:
    """Whole-table ``sector_map`` totals (rows / classified / distinct sectors), independent of the universe.
    Isolated so tests can monkeypatch it without a DB."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_SECTOR_MAP_TOTALS_QUERY)
        columns = [desc[0] for desc in cur.description]
        record = cur.fetchone()
        return dict(zip(columns, record)) if record is not None else {}


def _as_int(totals: dict[str, object], key: str) -> int:
    """A COUNT(*) cell as an int (0 when the totals read came back empty). The values are PG bigints; the
    dict is typed ``object`` only because it is built from a generic ``zip(columns, row)``."""
    value = totals.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def build_sector_coverage(
    universe_rows: list[dict[str, object]] | None = None,
    sector_map_totals: dict[str, object] | None = None,
) -> dict[str, object]:
    """The sector-coverage surface over the LIVE universe: per-sector counts + classified-vs-unknown split +
    classified % + a sample of unclassified tickers, plus whole-table ``sector_map`` totals.

    ``universe_rows`` / ``sector_map_totals`` default to the live DB reads; tests inject fixtures instead.

    Shape (see docs/SECTOR_COVERAGE.md):
      {generated_at, universe_date, universe_size, n_classified, n_unknown, classified_pct,
       n_blank_sector, n_no_row, n_distinct_sectors,
       sectors: [{sector, n_symbols, pct_of_universe}],   # ranked desc, classified sectors only
       unclassified_sample: [symbol, ...],
       sector_map: {n_rows, n_classified, n_distinct_sectors}}
    """
    rows = _read_universe_sectors() if universe_rows is None else universe_rows
    totals = _read_sector_map_totals() if sector_map_totals is None else sector_map_totals

    universe_size = len(rows)
    universe_date = rows[0].get("_universe_date") if rows else None

    sector_counts: dict[str, int] = {}
    n_classified = 0
    n_blank_sector = 0  # has a sector_map row but the sector label is blank/NULL
    n_no_row = 0  # no sector_map row at all
    unclassified: list[str] = []
    for record in rows:
        symbol = str(record["symbol"])
        if record["classified"]:
            n_classified += 1
            sector = str(record["sector"])
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        else:
            unclassified.append(symbol)
            if record["has_row"]:
                n_blank_sector += 1
            else:
                n_no_row += 1

    n_unknown = universe_size - n_classified
    classified_pct = round(100.0 * n_classified / universe_size, 1) if universe_size else 0.0

    sectors = [
        {
            "sector": sector,
            "n_symbols": count,
            "pct_of_universe": round(100.0 * count / universe_size, 1) if universe_size else 0.0,
        }
        for sector, count in sorted(sector_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "universe_date": universe_date,
        "universe_size": universe_size,
        "n_classified": n_classified,
        "n_unknown": n_unknown,
        "classified_pct": classified_pct,
        "n_blank_sector": n_blank_sector,
        "n_no_row": n_no_row,
        "n_distinct_sectors": len(sector_counts),
        "sectors": sectors,
        "unclassified_sample": sorted(unclassified)[:UNCLASSIFIED_SAMPLE],
        "sector_map": {
            "n_rows": _as_int(totals, "n_rows"),
            "n_classified": _as_int(totals, "n_classified"),
            "n_distinct_sectors": _as_int(totals, "n_distinct_sectors"),
        },
    }


class SectorCoverageCache:
    """Tiny TTL cache mirroring ``raw_coverage.RawCoverageCache`` — sector_map is slowly-changing (a weekly
    refresh at most) and the universe snapshot is daily, so a 60s TTL keeps a busy refresh instant while
    staying fresh enough for a coverage surface that barely moves."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._cached: tuple[float, dict[str, object]] | None = None

    def coverage(self, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        if not force and self._cached is not None and (now - self._cached[0]) <= self.ttl:
            return self._cached[1]
        view = build_sector_coverage()
        self._cached = (now, view)
        return view


CACHE = SectorCoverageCache()
