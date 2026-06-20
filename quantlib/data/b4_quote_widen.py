"""Compute the #208 B4 zero-quote widening set: the mid-liquidity tradeable names that have
bars + trades but NO quote tape yet.

Context (docs/TICKER_REPRESENTATION.md, PR #208): the deep+broad raw tape has COMPLETE trades
breadth but the QUOTES tier lags. After the liquid-core depth fill, the cheapest remaining breadth
win is the ~106 B4 names (ADV rank 2000-4000) that have zero quote partitions: a small, bounded set
that lifts B4 quote coverage from 95% toward 100%.

This module computes that set DETERMINISTICALLY from on-disk data at run time rather than pinning a
stale hardcoded list: it ranks the bars universe by dollar-volume (the SAME ranker the deep-backfill
uses, so the bands match every lane's `liquidity_bands` cut), slices the B4 band, and subtracts every
symbol that already has any quote-manifest coverage. The ops driver (`ops/quote_widen_b4.sh`) feeds
the result to `quantlib.data.raw_backfill --symbols ... --start ... --end ...` (WINDOW mode), which is
idempotent (manifest-skips already-fetched symbol-days) and memory-bounded.

ADV ranks are point-in-time on the most recent bars; a name near a band cut can move day to day, so
the set is approximate at the edges but stable in aggregate (the priority TIER is what matters).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from quantlib.data.raw_backfill import (MARKET_TICKERS, rank_by_dollar_volume,
                                        trading_client, trading_days)
from quantlib.data.raw_store import load_manifest

logger = logging.getLogger("b4_quote_widen")

# The liquidity bands every lane shares (docs/TICKER_REPRESENTATION.md): top-500 B1 / 500-1k B2 /
# 1k-2k B3 / 2k-4k B4 / 4k+ B5. B4 is the cheap-width target.
B4_RANK_START = 2000
B4_RANK_END = 4000
# Score liquidity over a recent window so the ranking is cheap and stable (matches the deep-backfill
# ranker default); the band tiering is invariant across nearby days.
RANK_LOOKBACK_DAYS = 30


def bars_universe(store: str) -> list[str]:
    """Every symbol with at least one bars partition on disk (the rankable universe).

    Read from the bars manifest, which records one row per fetched (symbol, date). Market reference
    tickers (SPY/QQQ) are excluded — they are pinned separately and are never a widening target."""
    manifest = load_manifest(store, "bars")
    if manifest is None or manifest.height == 0:
        return []
    symbols = manifest["symbol"].unique().to_list()
    market = set(MARKET_TICKERS)
    return sorted(symbol for symbol in symbols if symbol not in market)


def symbols_with_quotes(store: str) -> set[str]:
    """Every symbol that already has ANY quote-manifest coverage (do not re-target these)."""
    manifest = load_manifest(store, "quotes")
    if manifest is None or manifest.height == 0:
        return set()
    return set(manifest["symbol"].unique().to_list())


def compute_b4_zero_quote_set(
    store: str,
    end_day: dt.date,
    rank_lookback_days: int = RANK_LOOKBACK_DAYS,
) -> list[str]:
    """The B4 (ADV rank 2000-4000) names with bars but no quote tape, ranked most-liquid first.

    Deterministic from on-disk state: rank the bars universe by dollar-volume over the recent window,
    slice the B4 band, subtract anything already in the quotes manifest. Returns the ordered list
    (most-liquid first) so a budget-capped fetch covers the highest-value names first."""
    universe = bars_universe(store)
    if not universe:
        logger.warning("bars manifest empty — no B4 set to compute")
        return []

    client = trading_client()
    start = end_day - dt.timedelta(days=rank_lookback_days * 2 + 7)
    days = trading_days(client, start, end_day)
    if not days:
        logger.warning("no trading days in [%s, %s] — cannot rank", start, end_day)
        return []

    ranked = rank_by_dollar_volume(store, universe, days)
    b4_band = ranked[B4_RANK_START:B4_RANK_END]

    already = symbols_with_quotes(store)
    zero_quote = [symbol for symbol in b4_band if symbol not in already]
    logger.info(
        "B4 band [%d:%d] = %d names; %d already have quotes; %d zero-quote targets",
        B4_RANK_START,
        B4_RANK_END,
        len(b4_band),
        len(b4_band) - len(zero_quote),
        len(zero_quote),
    )
    return zero_quote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="/store")
    parser.add_argument(
        "--end",
        default=None,
        help="ADV-rank as-of date YYYY-MM-DD (default: today UTC)",
    )
    parser.add_argument(
        "--rank-lookback-days",
        type=int,
        default=RANK_LOOKBACK_DAYS,
        help="recent calendar window to score dollar-volume over",
    )
    args = parser.parse_args(argv)

    end_day = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date()
    targets = compute_b4_zero_quote_set(args.store, end_day, rank_lookback_days=args.rank_lookback_days)
    # Emit ONLY the comma-joined symbol list on stdout so the ops driver can capture it directly;
    # all human-readable progress goes to the logger (stderr).
    print(",".join(targets))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main())
