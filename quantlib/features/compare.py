"""Pure parity-comparison logic (no I/O) — shared by the minute and tick parity paths.

Kept free of DB/Alpaca imports so it is unit-testable on its own. ``diff`` dispatches on each
feature's declared ``parity_method`` (cell-wise tolerance, or distributional for tick-order-
sensitive Layer-C features).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import KEY_COLUMNS, BatchContext, FeatureGroup
from quantlib.features.engine import run_all
from quantlib.features.registry import REGISTRY

QUANTILES = (0.1, 0.5, 0.9)


def runnable(frames: dict[str, pl.DataFrame]) -> list[FeatureGroup]:
    """Groups whose every declared input is present — so a path runs only the right groups."""
    return [g for g in REGISTRY.groups() if all(spec.name in frames for spec in g.inputs)]


def vectors(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    return run_all(runnable(frames), BatchContext(frames=frames), validate=False)


def dist_score(scope: pl.DataFrame, feature: str, tol: float) -> tuple[float | None, bool | None]:
    """Distributional agreement: max relative gap between live & backfill quantiles (P10/P50/P90)."""
    pairs = scope.drop_nulls([feature, f"{feature}_bk"])
    if pairs.height == 0:
        return None, None
    max_reldiff = 0.0
    for quantile in QUANTILES:
        live_q = pairs.select(pl.col(feature).quantile(quantile)).item()
        back_q = pairs.select(pl.col(f"{feature}_bk").quantile(quantile)).item()
        if live_q is None or back_q is None:
            continue
        max_reldiff = max(max_reldiff, abs(live_q - back_q) / (abs(back_q) + 1e-9))
    return round(100.0 * (1.0 - min(1.0, max_reldiff)), 3), max_reldiff <= tol


def diff(live: pl.DataFrame, backfill: pl.DataFrame, tiers: pl.DataFrame) -> pl.DataFrame:
    """Per-feature, per-tier parity, dispatched on each feature's declared parity_method."""
    methods = {spec.name: spec.parity_method for _, spec in REGISTRY.feature_specs()}
    tolerances = {spec.name: spec.tolerance for _, spec in REGISTRY.feature_specs()}
    feature_cols = [c for c in live.columns if c not in KEY_COLUMNS]
    joined = live.join(backfill, on=list(KEY_COLUMNS), how="inner", suffix="_bk").join(
        tiers, on="symbol", how="left"
    ).with_columns(pl.col("tier").fill_null(3))

    rows = []
    for feature in feature_cols:
        live_col, back_col = pl.col(feature), pl.col(f"{feature}_bk")
        both = live_col.is_not_null() & back_col.is_not_null()
        if joined.schema[feature].is_float():
            both = both & live_col.is_not_nan() & back_col.is_not_nan()
        for tier in (1, 2, 3):
            scope = joined.filter(pl.col("tier") == tier)
            compared = int(scope.select(both.sum()).item() or 0)
            if methods[feature] == "distributional":
                score, passed = dist_score(scope, feature, tolerances[feature])
            else:
                matched = both & ((live_col - back_col).abs() <= tolerances[feature] * (1.0 + back_col.abs()))
                agree = int(scope.select(matched.sum()).item() or 0)
                score = round(100.0 * agree / compared, 3) if compared else None
                passed = score >= 95.0 if score is not None else None
            rows.append(
                {
                    "feature": feature,
                    "tier": tier,
                    "method": methods[feature],
                    "compared": compared,
                    "score": score,
                    "passed": (bool(passed) if compared else None),
                }
            )
    return pl.DataFrame(rows)
