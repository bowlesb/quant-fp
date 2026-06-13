"""Distribution + contract checks for computed features — introspection as a first-class citizen.

``introspect`` emits per-feature distribution stats; ``assert_sane`` FAILS loudly if any feature is
degenerate (< 2 unique non-null values), exceeds its declared range, or breaks its declared NaN cap
(FEATURE_PLATFORM.md §5). This is the automated half of the §1.3 "verify the distributions" step;
the QA agent's adversarial common-sense pass is the human-judgment half.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec

# Default NaN caps by nan_policy (FP_GOALS C) — a feature exceeding its cap is a defect to explain.
NAN_CAPS: dict[str, float] = {"none": 0.0, "warmup": 0.20, "sparse": 0.50}


class IntrospectionError(Exception):
    """Raised when a feature's realized distribution violates its declared contract."""


def _nan_fraction(out: pl.DataFrame, name: str) -> float:
    column = pl.col(name)
    missing = column.is_null()
    if out.schema[name].is_float():
        missing = missing | column.is_nan()
    return float(out.select((missing.sum() / pl.len())).item())


def introspect(out: pl.DataFrame, specs: list[FeatureSpec]) -> pl.DataFrame:
    """Per-feature distribution: count, nan_pct, min/p50/max, n_unique, and contract flags."""
    rows = []
    for spec in specs:
        series = out[spec.name]
        non_null = series.drop_nulls()
        n_unique = non_null.n_unique()
        nan_pct = _nan_fraction(out, spec.name)
        cap = NAN_CAPS.get(spec.nan_policy, 0.0)
        rows.append(
            {
                "feature": spec.name,
                "count": out.height,
                "nan_pct": round(nan_pct, 4),
                "nan_cap": cap,
                "min": _safe(series.min()),
                "p50": _safe(series.median()),
                "max": _safe(series.max()),
                "n_unique": n_unique,
                "degenerate": n_unique < 2,
                "range_violation": _range_violation(out, spec),
                "nan_over_cap": nan_pct > cap,
            }
        )
    return pl.DataFrame(rows)


def _safe(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _range_violation(out: pl.DataFrame, spec: FeatureSpec) -> bool:
    if spec.valid_range is None:
        return False
    low, high = spec.valid_range
    column = pl.col(spec.name)
    present = column.is_not_null()
    if out.schema[spec.name].is_float():
        present = present & column.is_not_nan()
    bounds = pl.lit(False)
    if low is not None:
        bounds = bounds | (column < low)
    if high is not None:
        bounds = bounds | (column > high)
    return bool(out.select((present & bounds).sum()).item())


def assert_sane(out: pl.DataFrame, specs: list[FeatureSpec]) -> pl.DataFrame:
    """Run introspection and RAISE if any feature is degenerate / out of range / over its NaN cap."""
    report = introspect(out, specs)
    bad = report.filter(
        pl.col("degenerate") | pl.col("range_violation") | pl.col("nan_over_cap")
    )
    if bad.height:
        failures = bad["feature"].to_list()
        raise IntrospectionError(f"introspection failed for {failures}:\n{bad}")
    return report
