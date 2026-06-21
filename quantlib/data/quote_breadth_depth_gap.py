"""Compute the BREADTH-AT-DEPTH quote gap: the broad-era names whose quote tape does NOT yet reach
back into an earlier target window.

Context (docs/TICKER_REPRESENTATION.md): the raw quote tape filled depth-first over a ~530-symbol
liquid HEAD (which reaches back into 2024-12) and only WIDENED to the broad ~3,950-symbol universe on
2026-03-18 (the breadth onset). So the broad breadth is a recent shelf: every non-head name has quotes
from 2026-03-18 forward but NONE before it. The head names already span the earlier dates.

This module computes, deterministically from the on-disk quotes manifest, the set of broad-era names
that are MISSING quote coverage in a requested earlier window — i.e. the names to extend BACKWARD. It is
the date-dimension sibling of `liquid_head_quote_gap.py` / `b4_quote_widen.py` (which fill names with
ZERO quote tape at all); here the names already have RECENT quotes, the gap is historical DEPTH.

Selection (no stale hardcoded list):
  * BROAD universe = the distinct symbols with a real quote tape (rows>0) on a settled reference broad
    date (default: a clean ~3,950-sym day at/after the breadth onset).
  * Subtract names that ALREADY cover the target window's START date (their tape already reaches back,
    e.g. the head set) — they would be fully manifest-skipped, so excluding them keeps the fetch list
    tight and the rank/log honest.
  * Emit the remainder. The downstream `raw_backfill` WINDOW fetch is idempotent per (symbol,date), so
    even a name that partially covers the window is safe to pass — the manifest skips its present cells.

The ops driver (`ops/quote_breadth_depth_fill.sh`) feeds the result to `quantlib.data.raw_backfill
--symbols ... --start ... --end ...` (WINDOW mode, quotes only, --processes 1, budget-capped). Re-running
is cheap and safe: once a name's window lands it drops out (its earliest covered date moves back).
"""

from __future__ import annotations

import argparse
import logging
import sys

import polars as pl

from quantlib.data.raw_backfill import MARKET_TICKERS
from quantlib.data.raw_store import load_manifest

logger = logging.getLogger("quote_breadth_depth_gap")

# A settled broad reference date: the quote breadth onset is 2026-03-18 (~3,945 syms); any clean day
# at/after it defines the same broad universe. Default to a day a few sessions past the onset so a
# still-settling onset day does not under-count.
DEFAULT_BROAD_REF_DATE = "2026-03-23"


def broad_universe(store: str, broad_ref_date: str) -> set[str]:
    """The distinct symbols with a real quote tape (rows>0) on the settled broad reference date.

    Market reference tickers (SPY/QQQ) are excluded — they are pinned separately and never a breadth
    target."""
    manifest = load_manifest(store, "quotes")
    if manifest is None or manifest.height == 0:
        return set()
    on_ref = manifest.filter((pl.col("date") == broad_ref_date) & (pl.col("rows") > 0))
    market = set(MARKET_TICKERS)
    return {symbol for symbol in on_ref["symbol"].unique().to_list() if symbol not in market}


def symbols_covering_date(store: str, window_start: str) -> set[str]:
    """Symbols whose quote tape ALREADY reaches back to (has a real-tape row on) the window start.

    These names already span the requested window, so the breadth-depth fetch would fully manifest-skip
    them — excluding them keeps the target list tight."""
    manifest = load_manifest(store, "quotes")
    if manifest is None or manifest.height == 0:
        return set()
    covering = manifest.filter((pl.col("date") <= window_start) & (pl.col("rows") > 0))
    return set(covering["symbol"].unique().to_list())


def compute_breadth_depth_gap(
    store: str,
    window_start: str,
    broad_ref_date: str = DEFAULT_BROAD_REF_DATE,
) -> list[str]:
    """The broad-era names missing quote coverage before ``window_start`` — the names to extend backward.

    Deterministic from the on-disk quotes manifest: take the broad universe (real tape on the reference
    broad date), subtract names already covering the window start. Returns a sorted, stable list."""
    broad = broad_universe(store, broad_ref_date)
    if not broad:
        logger.warning("no broad quote universe on ref date %s — nothing to extend", broad_ref_date)
        return []
    already = symbols_covering_date(store, window_start)
    gap = sorted(broad - already)
    logger.info(
        "broad universe (%s) = %d names; %d already reach <= %s; %d breadth-depth targets",
        broad_ref_date,
        len(broad),
        len(broad & already),
        window_start,
        len(gap),
    )
    return gap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="/store")
    parser.add_argument(
        "--window-start",
        required=True,
        help="target window start YYYY-MM-DD — emit broad names missing quotes before this date",
    )
    parser.add_argument(
        "--broad-ref-date",
        default=DEFAULT_BROAD_REF_DATE,
        help="settled broad day defining the ~3,950-sym universe (default: 2026-03-23)",
    )
    args = parser.parse_args(argv)

    targets = compute_breadth_depth_gap(args.store, args.window_start, broad_ref_date=args.broad_ref_date)
    # Emit ONLY the comma-joined symbol list on stdout so the ops driver can capture it; all
    # human-readable progress goes to the logger (stderr).
    print(",".join(targets))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main())
