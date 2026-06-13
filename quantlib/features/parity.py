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
from datetime import datetime

import polars as pl

from quantlib.features.backfill_ticks import load_trades_backfill
from quantlib.features.compare import diff, vectors
from quantlib.features.loaders import load_minute_agg, load_tiers, load_trades_live


def parity_test(day: str, source_live: str = "stream", source_backfill: str = "backfill") -> pl.DataFrame:
    """Minute-path (Layer A/B) T+1 parity for a settled day."""
    live = vectors({"minute_agg": load_minute_agg(day, source_live)})
    backfill = vectors({"minute_agg": load_minute_agg(day, source_backfill)})
    return diff(live, backfill, load_tiers(day))


def parity_test_ticks(start: datetime, end: datetime, symbols: list[str]) -> pl.DataFrame:
    """Layer-C tick-path parity: live captured ticks (trades_raw) vs settled Alpaca historical ticks."""
    live = vectors({"trades": load_trades_live(start, end, symbols)})
    backfill = vectors({"trades": load_trades_backfill(start, end, symbols)})
    tiers = pl.DataFrame(
        {"symbol": symbols, "tier": [1] * len(symbols)}, schema={"symbol": pl.String, "tier": pl.Int32}
    )
    return diff(live, backfill, tiers)


def _print(report: pl.DataFrame, title: str) -> None:
    pl.Config.set_tbl_rows(100)
    print(f"=== {title} ===")
    print(report)
    failed = report.filter(pl.col("passed") == False)  # noqa: E712 (Polars boolean filter)
    if failed.height:
        print(f"\nFAILED (feature,tier,method,score): {failed.select('feature','tier','method','score').rows()}")
    else:
        print("\nALL features/tiers with data PASS.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "ticks":
        day, start_hm, end_hm, syms = args[1], args[2], args[3], args[4].split(",")
        start = datetime.fromisoformat(f"{day}T{start_hm}:00+00:00")
        end = datetime.fromisoformat(f"{day}T{end_hm}:00+00:00")
        _print(parity_test_ticks(start, end, syms), f"Layer-C tick parity {day} {start_hm}-{end_hm}Z {syms}")
    elif args:
        _print(parity_test(args[0]), f"T+1 Settled-Day Parity {args[0]} (per-feature method)")
    else:
        raise SystemExit("usage: python -m quantlib.features.parity <day> | ticks <day> <HH:MM> <HH:MM> SYM,SYM")


if __name__ == "__main__":
    main()
