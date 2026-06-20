"""The T+1 Settled-Day Parity Test — the platform's cornerstone check (FEATURE_PLATFORM.md §3.5).

Wires the data sources (DB + Alpaca) into the pure comparison logic in ``compare.py``. Compute
every feature from the live-captured inputs AND the settled historical inputs through the IDENTICAL
group code, then diff per the feature's declared ``parity_method``. Groups self-select by available
inputs, so the minute-aggregate path and the raw-tick (Layer-C) path use the same engine.

Usage:
  python -m quantlib.features.parity <YYYY-MM-DD>                          # minute path (A/B)
  python -m quantlib.features.parity ticks <day> <HH:MM> <HH:MM> SYM,SYM   # Layer-C tick path (UTC)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import polars as pl

from quantlib.features import store
from quantlib.features.backfill_bars import backfill_daily
from quantlib.features.backfill_ticks import load_trades_backfill
from quantlib.features.compare import coverage, diff, vectors
from quantlib.features.loaders import (
    load_filings,
    load_minute_agg,
    load_reference,
    load_tiers,
    load_trades_live,
)
from quantlib.features.registry import REGISTRY


def parity_stored(root: str, day: str) -> pl.DataFrame:
    """THE direct Monday-vs-backfill check: diff the features we ACTUALLY COLLECTED LIVE (store
    source=stream, written by compute_latest during capture) against the features BACKFILL produced
    (store source=backfill, written by compute during materialize). Unlike parity_test (which recomputes
    both sides fresh from raw inputs via compute), this reads what we really wrote — so it catches BOTH
    input divergence AND any compute_latest-vs-compute drift on real data, in one shot. This is the test
    that earns the confidence to backfill further back: run it on the Monday overlap, expect ~100%."""
    start = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), tzinfo=timezone.utc)
    end = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 23, 59, 59, tzinfo=timezone.utc)
    names = REGISTRY.feature_names()
    live = store.get_features(names, "universe", start, end, root, source="stream")
    backfill = store.get_features(names, "universe", start, end, root, source="backfill")
    return diff(live, backfill, load_tiers(day))


def parity_test(day: str, source_live: str = "stream", source_backfill: str = "backfill") -> pl.DataFrame:
    """Minute-path (Layer A/B) T+1 parity for a settled day. The static reference snapshot AND the
    daily-history cache are fed to BOTH sides (both are source-independent settled artifacts), so the
    sector/asset-flag, multi-day and prior-day groups are covered too — and being identical they must
    score 100%, a standing check that those joins are deterministic and the daily broadcast is sound."""
    live_minute = load_minute_agg(day, source_live)
    backfill_minute = load_minute_agg(day, source_backfill)
    reference = load_reference()
    symbols = sorted(
        set(backfill_minute["symbol"].unique().to_list()) | set(live_minute["symbol"].unique().to_list())
    )
    daily = backfill_daily(day, symbols)
    tiers = load_tiers(day)
    universe = tiers.select("symbol")  # pin cross-sectional rank to the day's fixed membership (gap #3)
    # The EDGAR filings snapshot is fed to BOTH sides (source-independent, available_at fixed at first
    # sight), so edgar_filing_frequency is covered by the sweep and — being identical — must score 100%,
    # the standing check that the point-in-time DB join is deterministic.
    filings = load_filings(day)
    shared = {"reference": reference, "daily": daily, "universe": universe, "filings": filings}
    live = vectors({"minute_agg": live_minute, **shared})
    backfill = vectors({"minute_agg": backfill_minute, **shared})
    return diff(live, backfill, tiers)


def parity_test_ticks(start: datetime, end: datetime, symbols: list[str]) -> pl.DataFrame:
    """Layer-C tick-path parity: live captured ticks (trades_raw) vs settled Alpaca historical ticks."""
    live = vectors({"trades": load_trades_live(start, end, symbols)})
    backfill = vectors({"trades": load_trades_backfill(start, end, symbols)})
    tiers = pl.DataFrame(
        {"symbol": symbols, "tier": [1] * len(symbols)}, schema={"symbol": pl.String, "tier": pl.Int32}
    )
    return diff(live, backfill, tiers)


def _print(report: pl.DataFrame, title: str) -> bool:
    """Print the report; return True if any feature/tier with sufficient data FAILED parity."""
    pl.Config.set_tbl_rows(100)
    print(f"=== {title} ===")
    print(report)
    failed = report.filter(pl.col("passed") == False)  # noqa: E712 (Polars boolean filter)
    insufficient = report.filter(pl.col("passed").is_null() & (pl.col("compared") > 0))
    if insufficient.height:
        print(f"\nINSUFFICIENT SAMPLE (<100 cells, not certified): {insufficient.select('feature','tier','compared').rows()}")
    if failed.height:
        print(f"\nFAILED (feature,tier,score,coverage): {failed.select('feature','tier','score','coverage').rows()}")
    else:
        print("\nALL features/tiers with sufficient data PASS.")
    return bool(failed.height)


def main() -> None:
    args = sys.argv[1:]
    failed = False
    if args and args[0] == "stored":
        root, day = args[1], args[2]
        failed = _print(parity_stored(root, day), f"STORED live(stream) vs backfill — {day} (what we collected vs backfill)")
    elif args and args[0] == "ticks":
        day, start_hm, end_hm, syms = args[1], args[2], args[3], args[4].split(",")
        start = datetime.fromisoformat(f"{day}T{start_hm}:00+00:00")
        end = datetime.fromisoformat(f"{day}T{end_hm}:00+00:00")
        failed = _print(parity_test_ticks(start, end, syms), f"Layer-C tick parity {day} {start_hm}-{end_hm}Z {syms}")
    elif args and args[0] == "coverage":
        day = args[1]
        report = coverage(load_minute_agg(day, "stream"), load_minute_agg(day, "backfill"))
        pl.Config.set_tbl_rows(30)
        print(f"=== Missing-data coverage by ET hour — {day} (live stream vs settled backfill) ===")
        print(report)
    elif args:
        failed = _print(parity_test(args[0]), f"T+1 Settled-Day Parity {args[0]} (per-feature method)")
    else:
        raise SystemExit("usage: python -m quantlib.features.parity <day> | ticks <day> <HH:MM> <HH:MM> SYM,SYM")
    if failed:
        raise SystemExit(1)  # a divergence must fail loudly — a wrapping cron/certify gate depends on this


if __name__ == "__main__":
    main()
