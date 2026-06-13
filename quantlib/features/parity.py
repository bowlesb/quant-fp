"""The T+1 Settled-Day Parity Test — the platform's cornerstone check (FEATURE_PLATFORM.md §3.5).

Compute every registered feature from the live-captured inputs AND from the settled historical-API
inputs through the IDENTICAL group code, then diff cell-by-cell, per tier. A feature is trustworthy
only while this holds ≥95% per tier. Run as: ``python -m quantlib.features.parity <YYYY-MM-DD>``.
"""
from __future__ import annotations

import sys

import polars as pl

from quantlib.features.base import KEY_COLUMNS, BatchContext
from quantlib.features.engine import run_all
from quantlib.features.loaders import load_minute_agg, load_tiers
from quantlib.features.registry import REGISTRY


def _vectors(day: str, source: str) -> pl.DataFrame:
    ctx = BatchContext(frames={"minute_agg": load_minute_agg(day, source)})
    return run_all(REGISTRY.groups(), ctx, validate=False)


def _tolerances() -> dict[str, float]:
    """Each feature's declared relative parity tolerance (FeatureSpec.tolerance)."""
    return {spec.name: spec.tolerance for _, spec in REGISTRY.feature_specs()}


def parity_test(
    day: str, source_live: str = "stream", source_backfill: str = "backfill"
) -> pl.DataFrame:
    """Per-feature, per-tier match % between the live and settled-backfill feature computations."""
    live = _vectors(day, source_live)
    backfill = _vectors(day, source_backfill)
    tiers = load_tiers(day)

    feature_cols = [column for column in live.columns if column not in KEY_COLUMNS]
    tolerances = _tolerances()
    joined = live.join(backfill, on=list(KEY_COLUMNS), how="inner", suffix="_bk").join(
        tiers, on="symbol", how="left"
    ).with_columns(pl.col("tier").fill_null(3))

    rows = []
    for feature in feature_cols:
        tol = tolerances[feature]
        live_col, back_col = pl.col(feature), pl.col(f"{feature}_bk")
        both = live_col.is_not_null() & back_col.is_not_null()
        if joined.schema[feature].is_float():
            both = both & live_col.is_not_nan() & back_col.is_not_nan()
        matched = both & ((live_col - back_col).abs() <= tol * (1.0 + back_col.abs()))
        for tier in (1, 2, 3):
            scope = joined.filter(pl.col("tier") == tier)
            compared = int(scope.select(both.sum()).item() or 0)
            agree = int(scope.select(matched.sum()).item() or 0)
            rows.append(
                {
                    "feature": feature,
                    "tier": tier,
                    "compared": compared,
                    "matched": agree,
                    "match_pct": round(100.0 * agree / compared, 3) if compared else None,
                }
            )
    return pl.DataFrame(rows)


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else None
    if day is None:
        raise SystemExit("usage: python -m quantlib.features.parity <YYYY-MM-DD>")
    report = parity_test(day)
    pl.Config.set_tbl_rows(100)
    print(f"=== T+1 Settled-Day Parity — {day} (live=stream vs backfill, per-feature tolerance) ===")
    print(report)
    passing = report.filter(
        (pl.col("compared") > 0) & ((pl.col("match_pct") < 95.0) | pl.col("match_pct").is_null())
    )
    if passing.height:
        print(f"\nBELOW 95% (feature,tier): {passing.select('feature', 'tier', 'match_pct').rows()}")
    else:
        print("\nALL features/tiers with data >= 95% parity.")


if __name__ == "__main__":
    main()
