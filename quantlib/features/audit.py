"""Introspect a day's computed features (``make introspect``) — the distribution/contract audit.

Compute every feature for a day and print the per-feature distribution + contract flags
(degenerate / range / NaN-over-cap). Informational on real data (a thin trade feature is legitimately
sparse over the full bar universe until the substrate is symbol-scoped); the hard ``assert_sane``
gate runs on the certified substrate. Usage: ``python -m quantlib.features.audit <day> [source]``.
"""
from __future__ import annotations

import sys

import polars as pl

from quantlib.features.compare import runnable, vectors
from quantlib.features.introspect import introspect
from quantlib.features.loaders import load_filings, load_minute_agg, load_news_features


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m quantlib.features.audit <YYYY-MM-DD> [source]")
    day = sys.argv[1]
    source = sys.argv[2] if len(sys.argv) > 2 else "backfill"
    frames = {
        "minute_agg": load_minute_agg(day, source),
        "filings": load_filings(day),
        "news": load_news_features(day),
    }
    vector = vectors(frames)
    specs = [spec for group in runnable(frames) for spec in group.declare()]
    report = introspect(vector, specs)
    pl.Config.set_tbl_rows(100)
    pl.Config.set_tbl_cols(20)
    print(f"=== introspection — {day} ({source}) ===")
    print(report)
    flagged = report.filter(pl.col("degenerate") | pl.col("range_violation") | pl.col("nan_over_cap"))
    print(f"\nflagged features: {flagged['feature'].to_list() or 'none'}")


if __name__ == "__main__":
    main()
