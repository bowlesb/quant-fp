"""Pure parity-comparison logic (no I/O) — shared by the minute and tick parity paths.

Kept free of DB/Alpaca imports so it is unit-testable on its own. ``diff`` dispatches on each
feature's declared ``parity_method`` (cell-wise tolerance, or distributional for tick-order-
sensitive Layer-C features).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import KEY_COLUMNS, BatchContext, FeatureGroup, FeatureSpec, storage_dtype
from quantlib.features.engine import run_all
from quantlib.features.registry import REGISTRY

QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_EXTRA = "extra_live"  # live emitted a value, backfill did not — over-capture / busted minute
VERDICT_MISSING = "missing_live"  # backfill has it, live did not — capture gap (incl. legitimate warmup)
VERDICT_UNCOMPARED = "uncompared"  # both null — nothing to compare


def match_predicate(spec: FeatureSpec, live_col: pl.Expr, back_col: pl.Expr) -> pl.Expr:
    """The SINGLE 'these two cells agree' boolean — shared by ``diff`` and the validation ledger so the
    two can never drift (the ledger exists to prove agreement; it must use the exact predicate the
    parity report uses). Flag / integer-stored features must be EXACTLY equal — a 0/1 flag or a calendar
    int is right or wrong, never "within tolerance". Real-valued features use numpy-isclose semantics:
    a genuine RELATIVE tolerance with a tiny absolute floor (``|a-b| <= 1e-12 + tol*|b|``), pure-relative
    so small-scale features (realized_vol ~1e-3) aren't masked by an absolute floor that fakes a pass."""
    if storage_dtype(spec) in (pl.Float32, pl.Float64):
        return (live_col - back_col).abs() <= 1e-12 + spec.tolerance * back_col.abs()
    return live_col == back_col


def cell_verdict(spec: FeatureSpec, feature: str, schema: pl.Schema) -> pl.Expr:
    """Per-cell categorical verdict (match / mismatch / extra_live / missing_live / uncompared) for a
    tolerance-method feature, as ONE expression over a frame joined ``<feature>`` (live) + ``<feature>_bk``
    (backfill). Distributional features are NOT cell-verdicted — validate them at tier grain via
    ``dist_score`` (cell-for-cell is meaningless for tick-order-sensitive features by design)."""
    live_col, back_col = pl.col(feature), pl.col(f"{feature}_bk")
    live_present, back_present = live_col.is_not_null(), back_col.is_not_null()
    if schema[feature].is_float():
        live_present = live_present & live_col.is_not_nan()
    if schema[f"{feature}_bk"].is_float():
        back_present = back_present & back_col.is_not_nan()
    agree = match_predicate(spec, live_col, back_col)
    return (
        pl.when(live_present & back_present & agree).then(pl.lit(VERDICT_MATCH))
        .when(live_present & back_present).then(pl.lit(VERDICT_MISMATCH))
        .when(live_present).then(pl.lit(VERDICT_EXTRA))
        .when(back_present).then(pl.lit(VERDICT_MISSING))
        .otherwise(pl.lit(VERDICT_UNCOMPARED))
    )
# Below this a per-tier parity score is statistically meaningless (anti-gaming §6.4). 1000 is the
# ad-hoc sanity floor; FP0/FP3 CERTIFICATION uses the far higher 50k/100k cell floors (FP_GOALS).
MIN_PARITY_CELLS = 1000
# A feature must be COMPARABLE on this fraction of union cells, else parity is excluding too much
# (audit #7): can't certify on a 99% score over only 80% of the universe-minutes.
COVERAGE_FLOOR = 0.95


def runnable(frames: dict[str, pl.DataFrame]) -> list[FeatureGroup]:
    """Groups whose every declared input frame AND its declared columns are present — so a bars-only
    frame runs the bar groups (price/volatility/calendar) and skips the trade/quote groups."""
    out = []
    for group in REGISTRY.groups():
        if all(
            spec.name in frames and set(spec.columns) <= set(frames[spec.name].columns)
            for spec in group.inputs
        ):
            out.append(group)
    return out


def vectors(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    return run_all(runnable(frames), BatchContext(frames=frames), validate=False)


def dist_score(scope: pl.DataFrame, feature: str, tol: float) -> tuple[float | None, bool | None]:
    """Distributional agreement, PAIRED. Marginal-quantile match alone (the old version) passes a
    within-symbol SHUFFLE that has the same shape but disagrees cell-by-cell (audit #5). So we require
    BOTH: the quantile distributions agree (shape) AND most cells agree within a loose per-cell
    tolerance (pairing). The loose cell tolerance allows genuine tick-order sensitivity while still
    catching scrambled values."""
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
    live_col, back_col = pl.col(feature), pl.col(f"{feature}_bk")
    paired_frac = pairs.select(((live_col - back_col).abs() <= (3.0 * tol) * (1.0 + back_col.abs())).mean()).item()
    score = round(100.0 * (1.0 - min(1.0, max_reldiff)), 3)
    passed = (max_reldiff <= tol) and (float(paired_frac or 0.0) >= 0.80)
    return score, passed


def coverage(live: pl.DataFrame, backfill: pl.DataFrame) -> pl.DataFrame:
    """Missing-data detection (distinct from value parity): the cell-PRESENCE diff between sources,
    grouped by ET hour so the early-morning / after-hours sessions are visible. ``live_gaps`` =
    (symbol, minute) cells the settled backfill has that we did NOT capture live (a capture gap);
    ``live_extra`` = cells live has that backfill lacks (over-capture / busted-trade minutes)."""
    keys = list(KEY_COLUMNS)
    et_hour = pl.col("minute").dt.convert_time_zone("America/New_York").dt.hour().cast(pl.Int32).alias("et_hour")
    live_keys = live.select(keys).unique()
    backfill_keys = backfill.select(keys).unique()
    gaps = backfill_keys.join(live_keys, on=keys, how="anti").with_columns(et_hour)
    extra = live_keys.join(backfill_keys, on=keys, how="anti").with_columns(et_hour)
    by_hour = (
        backfill_keys.with_columns(et_hour).group_by("et_hour").agg(pl.len().alias("backfill_cells"))
        .join(gaps.group_by("et_hour").agg(pl.len().alias("live_gaps")), on="et_hour", how="left")
        .join(extra.group_by("et_hour").agg(pl.len().alias("live_extra")), on="et_hour", how="left")
        .fill_null(0)
        .sort("et_hour")
    )
    return by_hour.with_columns(
        (100.0 * (1.0 - pl.col("live_gaps") / pl.col("backfill_cells"))).round(3).alias("live_coverage_pct")
    )


def diff(live: pl.DataFrame, backfill: pl.DataFrame, tiers: pl.DataFrame) -> pl.DataFrame:
    """Per-feature, per-tier parity, dispatched on each feature's declared parity_method."""
    specs = {spec.name: spec for _, spec in REGISTRY.feature_specs()}
    methods = {name: spec.parity_method for name, spec in specs.items()}
    tolerances = {name: spec.tolerance for name, spec in specs.items()}
    feature_cols = [c for c in live.columns if c not in KEY_COLUMNS]
    joined = live.join(backfill, on=list(KEY_COLUMNS), how="full", suffix="_bk", coalesce=True).join(
        tiers, on="symbol", how="left"
    ).with_columns(pl.col("tier").fill_null(3))

    rows = []
    for feature in feature_cols:
        live_col, back_col = pl.col(feature), pl.col(f"{feature}_bk")
        live_present, back_present = live_col.is_not_null(), back_col.is_not_null()
        if joined.schema[feature].is_float():
            live_present = live_present & live_col.is_not_nan()
        if joined.schema[f"{feature}_bk"].is_float():
            back_present = back_present & back_col.is_not_nan()
        both, union = live_present & back_present, live_present | back_present
        for tier in (1, 2, 3):
            scope = joined.filter(pl.col("tier") == tier)
            compared = int(scope.select(both.sum()).item() or 0)
            union_count = int(scope.select(union.sum()).item() or 0)
            coverage = round(compared / union_count, 4) if union_count else None
            if methods[feature] == "distributional":
                score, raw_pass = dist_score(scope, feature, tolerances[feature])
            else:
                matched = both & match_predicate(specs[feature], live_col, back_col)
                agree = int(scope.select(matched.sum()).item() or 0)
                score = round(100.0 * agree / compared, 3) if compared else None
                raw_pass = score >= 95.0 if score is not None else None
            if compared < MIN_PARITY_CELLS:
                passed = None  # too few cells to judge
            elif coverage is None or coverage < COVERAGE_FLOOR:
                passed = False  # coverage gap -> not trustworthy; can't silently exclude the missing cells
            else:
                passed = bool(raw_pass) if raw_pass is not None else None
            rows.append(
                {
                    "feature": feature,
                    "tier": tier,
                    "method": methods[feature],
                    "compared": compared,
                    "coverage": coverage,
                    "score": score,
                    "passed": passed,
                }
            )
    return pl.DataFrame(rows)
