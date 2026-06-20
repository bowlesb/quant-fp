"""Compute the #208 NEXT quote tranche: the tight-spread (1-5bps) mega/large-cap LP-headroom names.

Context (docs/TICKER_REPRESENTATION.md, PR #208; sibling of ``b4_quote_widen``): after the B4 zero-quote
widening (the mid-liquidity 2000-4000 ADV band) landed, the remaining #208 quote-acquisition step is the
LIQUID HEAD — the mega/large-cap names (top ADV band) whose tight 1-5bps quoted spreads make them the
realistic surface for a liquidity-provision / market-making strategy. Quote DEPTH there is the substrate a
spread-capture or LP-headroom hunt needs, and it is the tape we have the least of historically.

Unlike B4 (ranked purely on bars dollar-volume), this tranche must be ranked on the ACTUAL quote tape:
``universe_membership.median_spread_bps`` is NULL (never seeded), so the spread tiering is measured from the
deep-quote panel itself. The selection is therefore two-stage and DETERMINISTIC from on-disk data:

  1. CANDIDATES — rank the bars universe by dollar-volume (the SAME ranker every lane shares, so the bands
     match ``liquidity_bands``) and take the liquid head (ADV rank < ``HEAD_RANK_END``). Mega/large caps live
     here; this is a cheap pre-filter so we only read quote tape for plausibly-tight names.
  2. SPREAD + HEADROOM — for each candidate that already has quote coverage, measure the MEDIAN relative
     spread ``(ask-bid)/mid`` in bps and a LP-headroom proxy (median top-of-book quoted size) over a recent
     sample of quote partitions. Keep names whose median spread falls in the tight ``[1, 5] bps`` band, then
     rank most-liquid-headroom first (deepest quoted size = most room to provide into).

The output is the ranked symbol list (stdout, comma-joined, for an ops driver) plus a human-readable
diagnostic table (stderr). It does NOT launch a backfill — it produces the target list the Lead can feed to
``quantlib.data.raw_backfill --symbols ... --start ... --end ...`` (idempotent, manifest-skipping,
memory-bounded), one tranche at a time, when the I/O slot is free.

Spread/headroom are point-in-time on a recent quote sample; a name near the 5bps cut can move day to day, so
the set is approximate at the edges but stable in the tier. Names already in the quotes manifest with NO
sampled tape (rows-0 settled-empty across the sample) are reported separately as "no-spread" — they are not
tight-spread LP candidates, they are illiquid.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import logging
import os
import sys

import polars as pl

from quantlib.data.raw_backfill import (MARKET_TICKERS, rank_by_dollar_volume,
                                        trading_client, trading_days)
from quantlib.data.raw_store import load_manifest

logger = logging.getLogger("next_quote_tranche")

# The liquid head of the shared liquidity bands (docs/TICKER_REPRESENTATION.md): top-500 B1 / 500-1k B2.
# Mega/large caps — the realistic 1-5bps LP surface — live in the top ~1000 by dollar-volume.
HEAD_RANK_END = 1000
# Score liquidity over a recent window so the candidate ranking is cheap and stable (matches b4_quote_widen
# / the deep-backfill ranker default); the band tiering is invariant across nearby days.
RANK_LOOKBACK_DAYS = 30
# Number of most-recent quote-coverage dates to sample per candidate when measuring spread/headroom. A
# handful of full quote days is enough to place a name in the 1-5bps tier robustly without reading its whole
# history (the median is stable; reading every partition would be a heavy tree walk).
SPREAD_SAMPLE_DATES = 5
# The tight-spread band that defines the LP tranche, in basis points of the mid.
TIGHT_SPREAD_BPS_MIN = 1.0
TIGHT_SPREAD_BPS_MAX = 5.0
# A relative spread above this is a data-glitch / locked-or-crossed artifact, not a real quote — exclude
# from the median so one bad tick does not disqualify a genuinely-tight name.
MAX_SANE_SPREAD_BPS = 500.0


def bars_universe(store: str) -> list[str]:
    """Every symbol with at least one bars partition on disk (the rankable candidate universe).

    Market reference tickers (SPY/QQQ) are excluded — they are pinned separately, never a tranche target."""
    manifest = load_manifest(store, "bars")
    if manifest.height == 0:
        return []
    symbols = manifest["symbol"].unique().to_list()
    market = set(MARKET_TICKERS)
    return sorted(symbol for symbol in symbols if symbol not in market)


def symbols_with_quotes(store: str) -> set[str]:
    """Every symbol that has ANY real quote-manifest coverage (rows > 0) — the names we can measure spread on
    from on-disk tape without re-fetching."""
    manifest = load_manifest(store, "quotes")
    if manifest.height == 0:
        return set()
    real = manifest.group_by("symbol").agg(pl.col("rows").max().alias("rows")).filter(pl.col("rows") > 0)
    return set(real["symbol"].to_list())


def recent_quote_dates(store: str, symbol: str, sample_dates: int) -> list[str]:
    """The most-recent ``sample_dates`` on-disk quote partition dates for a symbol (newest first)."""
    pattern = os.path.join(store, "raw", "quotes", f"symbol={symbol}", "date=*", "*.parquet")
    dates = []
    for path in glob.glob(pattern):
        for segment in path.split(os.sep):
            if segment.startswith("date="):
                dates.append(segment[len("date=") :])
    return sorted(set(dates), reverse=True)[:sample_dates]


def measure_spread_and_headroom(
    store: str, symbol: str, sample_dates: int = SPREAD_SAMPLE_DATES
) -> tuple[float, float, int] | None:
    """Median relative spread (bps) and a LP-headroom proxy (median top-of-book size) for one symbol over a
    recent quote sample. Returns ``(median_spread_bps, median_quoted_size, n_quotes)`` or ``None`` if no real
    quote rows were sampled (a settled-empty / illiquid name).

    Spread is ``(ask - bid) / mid * 1e4`` per quote, taken only over quotes with a positive, sane two-sided
    market (bid>0, ask>=bid, spread <= MAX_SANE_SPREAD_BPS) so a locked/crossed/glitch tick cannot skew the
    median. Headroom is the median of ``(bid_size + ask_size) / 2`` — deeper quoted size = more room for a
    provider to rest size without moving the touch."""
    dates = recent_quote_dates(store, symbol, sample_dates)
    if not dates:
        return None
    paths = [
        os.path.join(store, "raw", "quotes", f"symbol={symbol}", f"date={date}", "data.parquet")
        for date in dates
    ]
    existing = [path for path in paths if os.path.exists(path)]
    if not existing:
        return None
    frame = (
        pl.scan_parquet(existing)
        .select(["bid_price", "ask_price", "bid_size", "ask_size"])
        .filter((pl.col("bid_price") > 0) & (pl.col("ask_price") >= pl.col("bid_price")))
        .with_columns(
            (
                (pl.col("ask_price") - pl.col("bid_price"))
                / ((pl.col("ask_price") + pl.col("bid_price")) / 2.0)
                * 1e4
            ).alias("spread_bps"),
            ((pl.col("bid_size") + pl.col("ask_size")) / 2.0).alias("quoted_size"),
        )
        .filter(pl.col("spread_bps") <= MAX_SANE_SPREAD_BPS)
        .select(["spread_bps", "quoted_size"])
        .collect()
    )
    if frame.height == 0:
        return None
    median_spread = float(frame["spread_bps"].median() or 0.0)
    median_size = float(frame["quoted_size"].median() or 0.0)
    return median_spread, median_size, frame.height


def compute_next_tranche(
    store: str,
    end_day: dt.date,
    head_rank_end: int = HEAD_RANK_END,
    rank_lookback_days: int = RANK_LOOKBACK_DAYS,
    sample_dates: int = SPREAD_SAMPLE_DATES,
) -> tuple[list[str], pl.DataFrame]:
    """The tight-spread (1-5bps) liquid-head LP tranche, ranked deepest-headroom first.

    Returns ``(ordered_symbols, diagnostics)`` where diagnostics is a per-candidate frame
    (symbol, adv_rank, median_spread_bps, median_quoted_size, n_quotes_sampled, in_tranche). The ordered list
    is the tranche only (in_tranche), most quoted-headroom first, so a budget-capped fetch covers the deepest
    LP surface first."""
    universe = bars_universe(store)
    if not universe:
        logger.warning("bars manifest empty — no tranche to compute")
        return [], pl.DataFrame()

    client = trading_client()
    start = end_day - dt.timedelta(days=rank_lookback_days * 2 + 7)
    days = trading_days(client, start, end_day)
    if not days:
        logger.warning("no trading days in [%s, %s] — cannot rank", start, end_day)
        return [], pl.DataFrame()

    ranked = rank_by_dollar_volume(store, universe, days)
    head = ranked[:head_rank_end]
    with_quotes = symbols_with_quotes(store)
    candidates = [symbol for symbol in head if symbol in with_quotes]
    logger.info(
        "liquid head [:%d] = %d names; %d have quote tape to measure (others have no quotes yet)",
        head_rank_end,
        len(head),
        len(candidates),
    )

    rows: list[dict[str, object]] = []
    for adv_rank, symbol in enumerate(head):
        if symbol not in with_quotes:
            continue
        measured = measure_spread_and_headroom(store, symbol, sample_dates)
        if measured is None:
            rows.append(
                {
                    "symbol": symbol,
                    "adv_rank": adv_rank,
                    "median_spread_bps": None,
                    "median_quoted_size": None,
                    "n_quotes_sampled": 0,
                    "in_tranche": False,
                }
            )
            continue
        median_spread, median_size, n_quotes = measured
        in_tranche = TIGHT_SPREAD_BPS_MIN <= median_spread <= TIGHT_SPREAD_BPS_MAX
        rows.append(
            {
                "symbol": symbol,
                "adv_rank": adv_rank,
                "median_spread_bps": round(median_spread, 3),
                "median_quoted_size": round(median_size, 1),
                "n_quotes_sampled": n_quotes,
                "in_tranche": in_tranche,
            }
        )

    diagnostics = pl.DataFrame(rows) if rows else pl.DataFrame()
    if diagnostics.height == 0:
        return [], diagnostics

    tranche = diagnostics.filter(pl.col("in_tranche")).sort(
        "median_quoted_size", descending=True, nulls_last=True
    )
    ordered = tranche["symbol"].to_list()
    logger.info(
        "tight-spread [%g,%g]bps tranche = %d names (of %d measured candidates); ranked deepest-headroom first",
        TIGHT_SPREAD_BPS_MIN,
        TIGHT_SPREAD_BPS_MAX,
        len(ordered),
        diagnostics.filter(pl.col("n_quotes_sampled") > 0).height,
    )
    return ordered, diagnostics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="/store")
    parser.add_argument("--end", default=None, help="ADV-rank as-of date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--head-rank-end", type=int, default=HEAD_RANK_END)
    parser.add_argument("--rank-lookback-days", type=int, default=RANK_LOOKBACK_DAYS)
    parser.add_argument("--sample-dates", type=int, default=SPREAD_SAMPLE_DATES)
    parser.add_argument(
        "--show-diagnostics",
        action="store_true",
        help="print the full per-candidate spread/headroom table to stderr",
    )
    args = parser.parse_args(argv)

    end_day = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date()
    ordered, diagnostics = compute_next_tranche(
        args.store,
        end_day,
        head_rank_end=args.head_rank_end,
        rank_lookback_days=args.rank_lookback_days,
        sample_dates=args.sample_dates,
    )
    if args.show_diagnostics and diagnostics.height > 0:
        with pl.Config(tbl_rows=-1):
            logger.info("per-candidate diagnostics:\n%s", diagnostics.sort("adv_rank"))
    # Emit ONLY the comma-joined tranche symbols on stdout so an ops driver can capture it directly; all
    # human-readable progress goes to the logger (stderr).
    print(",".join(ordered))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main())
