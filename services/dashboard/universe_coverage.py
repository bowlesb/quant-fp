"""UNIVERSE coverage aggregation — captured-vs-available breadth over time, the whole-universe ratio.

The live feature-capture universe is seeded each session into ``universe_membership`` (the capped, in-universe
symbol SET for a ``trade_date``; see ``quantlib.features.seed_universe``). The AVAILABLE filtered set is every
tradable common stock on a primary US venue that survives the same exchange + ETF/fund screen the seed applies
(``KEEP_EXCHANGES`` + ``is_etf_like`` over ``asset_metadata``, the uncapped per-asset reference Alpaca pulls).
The gap between the two is the breadth that COULD be captured but isn't.

That gap was a real, capital-relevant regression (06-16 relaunch silently re-capped capture at the default
``UNIVERSE_MAX_SYMBOLS=3000`` of ~7.3k available — a 41% surface, caught by the #223 coverage-DROP detector and
Lead-root-caused). #223 catches a GROUP thinning relative to its own peak; this is its whole-universe complement
— per day, how many symbols are ACTUALLY in the captured universe vs how many COULD be — so a future silent
re-cap is an instantly-visible, permanent dashboard fixture, not a one-time catch.

This module makes it legible. It reads:
  * ``universe_membership`` — the per-day captured ``in_universe`` count (the CAPTURED breadth over time), and
  * ``asset_metadata`` — every pulled asset, screened in Python with the EXACT seed filter
    (``KEEP_EXCHANGES`` + ``is_etf_like``) to reproduce the AVAILABLE filtered set the seed would cap from.

and reports, per day and as a headline:
  * captured count, available count, captured / available RATIO (the breadth %),
  * the latest-day gap (available − captured = uncaptured names left on the table),
  * a small per-day timeline so a re-cap / restore shows up as a step in the ratio.

The available set is a SNAPSHOT (asset_metadata is refreshed each seed run, no per-day history), so it is the
SAME denominator for every day; the captured numerator is genuinely per-day. A captured count ABOVE the current
available denominator (e.g. 06-15's pre-ETF-filter 11336 seed) is flagged, not hidden — it means that day's seed
predated the current screen, so its ratio is informational only.

READ-ONLY. It only ``SELECT``s from ``universe_membership`` and ``asset_metadata`` and applies the existing pure
``is_etf_like`` screen — NO schema/format change, no write, NO store I/O. The two DB reads are isolated into
helper functions so tests can monkeypatch them without a DB.
"""

from __future__ import annotations

import datetime as dt
import os
import time

from quantlib.features.seed_universe import KEEP_EXCHANGES
from quantlib.universe import is_etf_like

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# How many most-recent captured days to surface in the timeline (weekdays only land, so ~6 weeks of sessions).
TIMELINE_DEFAULT_DAYS = 30

# captured/available ratio thresholds for the headline status badge (matches the spirit of #223's drop bands).
RATIO_OK = 0.90  # at/above this = effectively full breadth
RATIO_THIN = 0.60  # below OK but above this = thinned; below this = a hard cap/regression


_CAPTURED_PER_DAY_QUERY = """
SELECT trade_date, count(*) FILTER (WHERE in_universe) AS n_captured
FROM universe_membership
GROUP BY trade_date
ORDER BY trade_date DESC
LIMIT %s
"""

# Every pulled asset's screen inputs. The ETF/fund screen is a Python regex (is_etf_like), so the filter is
# applied in Python here rather than in SQL — byte-for-byte the seed's select_universe logic, so the available
# count reproduces what the seed would cap from.
_ASSET_METADATA_QUERY = "SELECT symbol, name, exchange, tradable FROM asset_metadata"


def _read_captured_per_day(days: int) -> list[dict[str, object]]:
    """Per-day captured (``in_universe``) counts, newest first. Isolated so tests monkeypatch it without a DB."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_CAPTURED_PER_DAY_QUERY, (days,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, record)) for record in cur.fetchall()]


def _read_asset_metadata() -> list[dict[str, object]]:
    """Every pulled asset's symbol/name/exchange/tradable for the available-set screen. Isolated for tests."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_ASSET_METADATA_QUERY)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, record)) for record in cur.fetchall()]


def _exchange_str(exchange: object) -> str:
    """The exchange code as a plain string (asset_metadata stores text; tolerate None)."""
    return str(exchange) if exchange is not None else ""


def available_filtered_count(asset_rows: list[dict[str, object]]) -> int:
    """The AVAILABLE filtered set size — every tradable, primary-venue, non-ETF-like, non-slash symbol. This is
    EXACTLY ``seed_universe.select_universe``'s screen (minus the MAX_SYMBOLS cap), so it reproduces the full
    set the seed would cap from."""
    available = {
        str(record["symbol"])
        for record in asset_rows
        if bool(record["tradable"])
        and "/" not in str(record["symbol"])
        and _exchange_str(record["exchange"]) in KEEP_EXCHANGES
        and not is_etf_like(record["name"] if isinstance(record["name"], str) else None)
    }
    return len(available)


def _ratio_status(ratio: float) -> str:
    """Headline status band for a captured/available ratio (mirrors the #223 drop bands)."""
    if ratio >= RATIO_OK:
        return "full"
    if ratio >= RATIO_THIN:
        return "thinned"
    return "capped"


def build_universe_coverage(
    captured_rows: list[dict[str, object]] | None = None,
    asset_rows: list[dict[str, object]] | None = None,
    days: int = TIMELINE_DEFAULT_DAYS,
) -> dict[str, object]:
    """The universe-coverage surface: captured (``universe_membership``) vs available (filtered
    ``asset_metadata``) breadth over time + the headline ratio + a per-day timeline.

    ``captured_rows`` / ``asset_rows`` default to the live DB reads; tests inject fixtures instead.

    Shape (see docs/UNIVERSE_COVERAGE.md):
      {generated_at, available, latest: {date, captured, ratio, ratio_pct, uncaptured, status, over_available},
       status, ratio_thresholds: {ok, thin},
       timeline: [{date, captured, ratio, ratio_pct, uncaptured, status, over_available}]}   # newest first
    """
    captured = _read_captured_per_day(days) if captured_rows is None else captured_rows
    assets = _read_asset_metadata() if asset_rows is None else asset_rows

    available = available_filtered_count(assets)

    timeline: list[dict[str, object]] = []
    for record in captured:
        trade_date = record["trade_date"]
        date_str = trade_date.isoformat() if isinstance(trade_date, dt.date) else str(trade_date)
        n_captured = int(record["n_captured"]) if record["n_captured"] is not None else 0
        # The available set is a current snapshot used as the SAME denominator for every day; a day captured
        # ABOVE it (a pre-screen seed like 06-15's 11336) is flagged via over_available, ratio clamped to 1.0.
        over_available = available > 0 and n_captured > available
        ratio = 1.0 if over_available else (n_captured / available if available > 0 else 0.0)
        uncaptured = max(0, available - n_captured)
        timeline.append(
            {
                "date": date_str,
                "captured": n_captured,
                "ratio": round(ratio, 4),
                "ratio_pct": round(100.0 * ratio, 1),
                "uncaptured": uncaptured,
                "status": _ratio_status(ratio),
                "over_available": over_available,
            }
        )

    latest = timeline[0] if timeline else None
    status = str(latest["status"]) if latest is not None else "unknown"

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "available": available,
        "latest": latest,
        "status": status,
        "ratio_thresholds": {"ok": RATIO_OK, "thin": RATIO_THIN},
        "timeline": timeline,
    }


class UniverseCoverageCache:
    """Tiny TTL cache mirroring ``sector_coverage.SectorCoverageCache`` — universe_membership is daily and
    asset_metadata is a per-seed refresh, so a 60s TTL keeps a busy refresh instant while staying fresh."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._cached: tuple[float, int, dict[str, object]] | None = None

    def coverage(self, days: int = TIMELINE_DEFAULT_DAYS, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        if (
            not force
            and self._cached is not None
            and self._cached[1] == days
            and (now - self._cached[0]) <= self.ttl
        ):
            return self._cached[2]
        view = build_universe_coverage(days=days)
        self._cached = (now, days, view)
        return view


CACHE = UniverseCoverageCache()
