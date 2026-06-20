"""Compute the LIQUID-HEAD zero-quote set: the most-liquid tradeable names that have a bars tape
but NO quote tape at all.

Context (docs/TICKER_REPRESENTATION.md): the deep+broad raw tape filled quotes depth-first over the
liquid core and then widened the B4 mid-liquidity band (PR #217). A residual gap remained at the very
TOP of the liquidity ranking — a handful of high-ADV names (the SPDR sector ETFs XLK/XLE/XLF/... chief
among them) that have full bars coverage but zero quote partitions, because they sit outside the
trades+quotes universe the deep-backfill ranked. These are the HIGHEST-value missing quotes on the
tape: the sector ETFs are the canonical market-regime / sector-rotation conditioners, and any
quote-spread cost model wants the liquid head first.

This module computes that set DETERMINISTICALLY from on-disk state at run time (no stale hardcoded
list): rank the bars universe by dollar-volume (the SAME `rank_by_dollar_volume` the deep-backfill and
b4_quote_widen use, so the band cuts match every lane), take the liquid head (`rank < head_rank`), and
subtract every symbol that already has any quote-manifest coverage. The ops driver
(`ops/quote_fill_liquid_head.sh`) feeds the result to `quantlib.data.raw_backfill --symbols ...
--start ... --end ...` (WINDOW mode), which is idempotent (manifest-skips already-fetched symbol-days)
and memory-bounded.

This is the liquid-HEAD sibling of `b4_quote_widen.py` (which targets the rank 2000-4000 mid band);
together they close the quote-breadth gap from both ends. Re-running is safe and cheap: once a name's
quotes land it drops out of the computed set on the next run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from quantlib.data.raw_backfill import (MARKET_TICKERS, rank_by_dollar_volume,
                                        trading_client, trading_days)
from quantlib.data.raw_store import load_manifest

logger = logging.getLogger("liquid_head_quote_gap")

# The liquid head — top of the shared dollar-volume ranking (docs/TICKER_REPRESENTATION.md bands B1/B2
# are rank 0-1000). A name above this rank with no quote tape is a high-priority gap; below it the
# b4_quote_widen mid-band (2000-4000) and the deep-quote breadth fill already cover the tape.
DEFAULT_HEAD_RANK = 1000
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


def compute_liquid_head_zero_quote_set(
    store: str,
    end_day: dt.date,
    head_rank: int = DEFAULT_HEAD_RANK,
    rank_lookback_days: int = RANK_LOOKBACK_DAYS,
) -> list[str]:
    """The liquid-head (ADV rank < head_rank) names with bars but no quote tape, most-liquid first.

    Deterministic from on-disk state: rank the bars universe by dollar-volume over the recent window,
    take the head slice, subtract anything already in the quotes manifest. Returns the ordered list
    (most-liquid first) so a budget-capped fetch covers the highest-value names first."""
    universe = bars_universe(store)
    if not universe:
        logger.warning("bars manifest empty — no liquid-head set to compute")
        return []

    client = trading_client()
    start = end_day - dt.timedelta(days=rank_lookback_days * 2 + 7)
    days = trading_days(client, start, end_day)
    if not days:
        logger.warning("no trading days in [%s, %s] — cannot rank", start, end_day)
        return []

    ranked = rank_by_dollar_volume(store, universe, days)
    head = ranked[:head_rank]

    already = symbols_with_quotes(store)
    zero_quote = [symbol for symbol in head if symbol not in already]
    logger.info(
        "liquid head [rank<%d] = %d names; %d already have quotes; %d zero-quote targets: %s",
        head_rank,
        len(head),
        len(head) - len(zero_quote),
        len(zero_quote),
        ",".join(zero_quote),
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
        "--head-rank",
        type=int,
        default=DEFAULT_HEAD_RANK,
        help="liquid-head cutoff: include zero-quote names with dollar-volume rank below this",
    )
    parser.add_argument(
        "--rank-lookback-days",
        type=int,
        default=RANK_LOOKBACK_DAYS,
        help="recent calendar window to score dollar-volume over",
    )
    args = parser.parse_args(argv)

    end_day = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date()
    targets = compute_liquid_head_zero_quote_set(
        args.store,
        end_day,
        head_rank=args.head_rank,
        rank_lookback_days=args.rank_lookback_days,
    )
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
