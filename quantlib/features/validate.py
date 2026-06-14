"""The validation job — certify real-time-collected features against an equivalent backfill, durably.

Reads what we ACTUALLY stored live (``source=stream``, written by ``compute_latest`` during capture)
and what backfill produced (``source=backfill``, written by ``compute`` during materialize), classifies
every cell (match / mismatch / extra_live / missing_live) via the SHARED ``compare.cell_verdict``
predicate, writes the durable verdict layers (``validation_store``), and recomputes the per-feature
trust registration. This is the operational "real thing" (docs/VALIDATION_LEDGER.md), not the ephemeral
dev-time ``compute_latest()==compute()`` unit check.

Both sides are PINNED to the day's universe membership (``load_tiers``) before comparison — the live
subscription and the backfill resolve their symbol sets at different times, so an unpinned diff compares
two non-aligned universes and manufactures a false ~20% coverage gap (root-caused 2026-06-14). Pinning
to the day's fixed membership is what ``parity_test`` already does and the ledger must do the same.

Scope honesty: certification here proves the live compute path reproduces backfill on RECENT overlap
data — NOT that deep-history backfill equals what live would have collected (point-in-time reference,
splits, vendor tape revisions drift). We prioritize ticker breadth over temporal depth and are honest
about the deep-history limit. ``status=certified`` means overlap-certified, nothing more.

Usage:
  python -m quantlib.features.validate <YYYY-MM-DD> <feature_store_root> <validation_root> [--allow-today]
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import polars as pl

from quantlib.features import store, validation_store
from quantlib.features.base import KEY_COLUMNS
from quantlib.features.compare import (
    VERDICT_EXTRA,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISSING,
    cell_verdict,
    dist_score,
)
from quantlib.features.loaders import load_tiers
from quantlib.features.registry import REGISTRY
from quantlib.features.session import rth_mask

# Trust grade thresholds on the value-match rate (fraction of compared cells that agree).
GRADE_THRESHOLDS: tuple[tuple[str, float], ...] = (("A", 0.9999), ("B", 0.999), ("C", 0.99))
MIN_DAYS_TO_CERTIFY = 5  # below this a feature is still `validating` (too little history to trust)
HARD_FLOOR = 0.95  # a single day below this flips `divergent` immediately (a broken feature can't stay certified)
CERTIFY_GRADES = ("A", "B")  # both value and coverage must reach one of these to certify


def assert_settled(day: str, allow_today: bool) -> None:
    """Refuse to validate a day whose capture may still be running — a partial live day reads as a wall
    of false ``missing_live``. The settled (T+1) contract mirrors ``store.get_features(require_settled)``."""
    today_et = datetime.now(timezone.utc).astimezone().date().isoformat()
    if not allow_today and day >= today_et:
        raise ValueError(
            f"refusing to validate {day}: not settled (>= today {today_et}); capture may be in progress. "
            f"validate T+1, or pass allow_today for a closed-session test."
        )


def grade_for(rate: float | None) -> str:
    """Letter grade for a match/coverage rate; 'U' (unvalidated) when there's nothing to grade."""
    if rate is None:
        return "U"
    for letter, threshold in GRADE_THRESHOLDS:
        if rate >= threshold:
            return letter
    return "F"


def _normalize_pairs(joined: pl.DataFrame, feature: str) -> pl.DataFrame:
    """Ensure both the live (``feature``) and backfill (``feature``_bk) columns exist as Float64 — a
    feature captured by only one source still gets compared (all-missing or all-extra), never skipped."""
    casts = []
    for col in (feature, f"{feature}_bk"):
        if col not in joined.columns:
            joined = joined.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
        casts.append(pl.col(col).cast(pl.Float64).alias(col))
    return joined.with_columns(casts)


def _long_verdicts(joined: pl.DataFrame, tol_features: list[str], specs: dict) -> pl.DataFrame:
    """Per-cell long frame (symbol, minute, tier, feature, live, back, verdict) for tolerance features —
    one vertical block per feature (== an unpivot, but lets each feature carry its own match predicate)."""
    parts = []
    for feature in tol_features:
        frame = _normalize_pairs(joined, feature)
        parts.append(
            frame.select(
                *KEY_COLUMNS,
                "tier",
                pl.lit(feature).alias("feature"),
                pl.col(feature).alias("live"),
                pl.col(f"{feature}_bk").alias("back"),
                cell_verdict(specs[feature], feature, frame.schema).alias("verdict"),
            )
        )
    return pl.concat(parts) if parts else pl.DataFrame()


def _cell_rollup(long: pl.DataFrame) -> pl.DataFrame:
    """Layer-2: per (feature, symbol, tier) counts + worst error — the drill-down detail."""
    abs_err = (pl.col("live") - pl.col("back")).abs()
    return long.group_by("feature", "symbol", "tier").agg(
        (pl.col("verdict") == VERDICT_MATCH).sum().alias("n_match"),
        (pl.col("verdict") == VERDICT_MISMATCH).sum().alias("n_mismatch"),
        (pl.col("verdict") == VERDICT_EXTRA).sum().alias("n_extra_live"),
        (pl.col("verdict") == VERDICT_MISSING).sum().alias("n_missing_live"),
        abs_err.filter(pl.col("verdict") == VERDICT_MISMATCH).max().alias("worst_abs_err"),
    )


def _exceptions(long: pl.DataFrame, day: str) -> pl.DataFrame:
    """Layer-1: the actual diverging cells (mismatch + extra_live), with values — rare by design."""
    bad = long.filter(pl.col("verdict").is_in([VERDICT_MISMATCH, VERDICT_EXTRA]))
    if bad.height == 0:
        return pl.DataFrame()
    abs_err = (pl.col("live") - pl.col("back")).abs()
    return bad.select(
        pl.lit(day).alias("day"),
        "feature",
        "symbol",
        "minute",
        "tier",
        pl.col("verdict").alias("status"),
        pl.col("live").alias("stream_value"),
        pl.col("back").alias("backfill_value"),
        abs_err.alias("abs_err"),
        (abs_err / (pl.col("back").abs() + 1e-12)).alias("rel_err"),
    )


def _feature_day_tolerance(cell: pl.DataFrame, version_of: dict, nan_policy_of: dict, day: str) -> pl.DataFrame:
    """Aggregate the per-symbol rollup to the durable per-(version, feature, day) trust source."""
    agg = cell.group_by("feature").agg(
        pl.col("n_match").sum(),
        pl.col("n_mismatch").sum(),
        pl.col("n_extra_live").sum(),
        pl.col("n_missing_live").sum(),
        pl.col("worst_abs_err").max(),
    )
    n_compared = pl.col("n_match") + pl.col("n_mismatch")
    return agg.with_columns(
        pl.col("feature").replace_strict(version_of, default="unknown").alias("version"),
        pl.lit(day).alias("day"),
        pl.lit("tolerance").alias("method"),
        pl.col("feature").replace_strict(nan_policy_of, default="none").alias("nan_policy"),
        n_compared.alias("n_compared"),
        pl.when(n_compared > 0).then((pl.col("n_match") / n_compared)).otherwise(None).alias("value_rate"),
        pl.when((n_compared + pl.col("n_missing_live")) > 0)
        .then(n_compared / (n_compared + pl.col("n_missing_live")))
        .otherwise(None)
        .alias("coverage_rate"),
        pl.col("worst_abs_err").alias("worst_abs_err"),
    )


def _feature_day_distributional(joined: pl.DataFrame, feature: str, tol: float, version: str, nan_policy: str, day: str) -> dict:
    """Distributional features are validated at tier grain via dist_score (cell-for-cell is meaningless
    for tick-order-sensitive features) — paired_frac IS their match rate. No per-cell exceptions."""
    frame = _normalize_pairs(joined, feature)
    n_paired, n_agree = 0, 0
    for tier in (1, 2, 3):
        scope = frame.filter(pl.col("tier") == tier)
        _, passed = dist_score(scope, feature, tol)
        pairs = scope.drop_nulls([feature, f"{feature}_bk"]).height
        n_paired += pairs
        if passed:
            n_agree += pairs  # a passing tier counts its paired cells as agreeing (shape + pairing held)
    return {
        "feature": feature,
        "version": version,
        "day": day,
        "method": "distributional",
        "nan_policy": nan_policy,
        "n_match": n_agree,
        "n_mismatch": n_paired - n_agree,
        "n_extra_live": 0,
        "n_missing_live": 0,
        "n_compared": n_paired,
        "value_rate": (n_agree / n_paired) if n_paired else None,
        "coverage_rate": 1.0 if n_paired else None,
        "worst_abs_err": None,
    }


def recompute_trust(feature_day: pl.DataFrame) -> pl.DataFrame:
    """Pure recompute of the per-feature trust registration from the durable feature_day table —
    idempotent and self-healing (re-validating a day just changes its feature_day rows)."""
    if feature_day.height == 0:
        return pl.DataFrame()
    last_day = (
        feature_day.sort("day")
        .group_by("version", "feature")
        .agg(pl.col("value_rate").last().alias("last_day_value_rate"),
             pl.col("coverage_rate").last().alias("last_day_coverage_rate"),
             pl.col("day").last().alias("last_validated_day"))
    )
    rolled = feature_day.group_by("version", "feature").agg(
        pl.col("method").first(),
        pl.col("nan_policy").first(),
        pl.col("day").n_unique().alias("n_days_validated"),
        pl.col("n_compared").sum().alias("lifetime_compared"),
        pl.col("n_match").sum().alias("lifetime_match"),
        pl.col("n_missing_live").sum().alias("lifetime_missing"),
        pl.col("worst_abs_err").max(),
    ).join(last_day, on=["version", "feature"], how="left")

    value_rate = pl.when(pl.col("lifetime_compared") > 0).then(
        pl.col("lifetime_match") / pl.col("lifetime_compared")
    ).otherwise(None)
    coverage_rate = pl.when((pl.col("lifetime_compared") + pl.col("lifetime_missing")) > 0).then(
        pl.col("lifetime_compared") / (pl.col("lifetime_compared") + pl.col("lifetime_missing"))
    ).otherwise(None)
    rolled = rolled.with_columns(
        value_rate.alias("lifetime_value_rate"), coverage_rate.alias("lifetime_coverage_rate")
    )
    return rolled.with_columns(
        pl.struct("lifetime_value_rate", "lifetime_coverage_rate", "n_days_validated",
                  "last_day_value_rate", "nan_policy")
        .map_elements(_status_of, return_dtype=pl.String)
        .alias("status"),
        pl.col("lifetime_value_rate").map_elements(grade_for, return_dtype=pl.String).alias("value_grade"),
        pl.col("lifetime_coverage_rate").map_elements(grade_for, return_dtype=pl.String).alias("coverage_grade"),
    ).sort("version", "feature")


def _status_of(row: dict) -> str:
    """Trust status from the lifetime grades + history. Warmup-policy features are not gated on coverage
    (their morning missing_live is correct by construction; v1 does not subtract a per-window allowance)."""
    last_value = row["last_day_value_rate"]
    if last_value is not None and last_value < HARD_FLOOR:
        return "divergent"
    if (row["n_days_validated"] or 0) < MIN_DAYS_TO_CERTIFY:
        return "validating"
    value_ok = grade_for(row["lifetime_value_rate"]) in CERTIFY_GRADES
    coverage_ok = row["nan_policy"] == "warmup" or grade_for(row["lifetime_coverage_rate"]) in CERTIFY_GRADES
    return "certified" if (value_ok and coverage_ok) else "divergent"


def validate(feature_root: str, day: str, val_root: str, allow_today: bool = False) -> pl.DataFrame:
    """Validate one settled day's stored stream vs backfill, write the ledger, recompute trust, return it."""
    assert_settled(day, allow_today)
    specs = {spec.name: spec for _, spec in REGISTRY.feature_specs()}
    version_of = {spec.name: group.version for group, spec in REGISTRY.feature_specs()}
    nan_policy_of = {name: spec.nan_policy for name, spec in specs.items()}
    tiers = load_tiers(day)
    symbols = tiers["symbol"].to_list()  # PIN both sides to the day's universe membership
    if not symbols:
        raise ValueError(f"no universe membership for {day} — cannot validate (build_universe must have run)")
    start = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), tzinfo=timezone.utc)
    end = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 23, 59, 59, tzinfo=timezone.utc)

    feature_day_rows: list[pl.DataFrame] = []
    dist_rows: list[dict] = []
    cell_blocks: list[pl.DataFrame] = []
    exception_blocks: list[pl.DataFrame] = []
    for group in REGISTRY.groups():
        feats = [spec.name for spec in group.declare()]
        backfill = store.get_features(feats, symbols, start, end, feature_root, source="backfill")
        if backfill.height == 0:
            continue  # no settled backfill for this group/day -> unvalidated, skip (per-group settled check)
        live = store.get_features(feats, symbols, start, end, feature_root, source="stream")
        if live.height == 0:
            live = backfill.select(KEY_COLUMNS).clear()  # nothing captured live -> all missing_live
        joined = (
            live.join(backfill, on=list(KEY_COLUMNS), how="full", suffix="_bk", coalesce=True)
            .join(tiers, on="symbol", how="left")
            .with_columns(pl.col("tier").fill_null(3))
            .filter(rth_mask(pl.col("minute")))  # bet/validate only during RTH — warmup stays out of the grade
        )
        tol_feats = [f for f in feats if specs[f].parity_method != "distributional"]
        dist_feats = [f for f in feats if specs[f].parity_method == "distributional"]
        if tol_feats:
            long = _long_verdicts(joined, tol_feats, specs)
            cell = _cell_rollup(long)
            cell_blocks.append(cell)
            exception_blocks.append(_exceptions(long, day))
            feature_day_rows.append(_feature_day_tolerance(cell, version_of, nan_policy_of, day))
        for feature in dist_feats:
            dist_rows.append(
                _feature_day_distributional(joined, feature, specs[feature].tolerance,
                                            version_of[feature], nan_policy_of[feature], day)
            )

    feature_day = _assemble_feature_day(feature_day_rows, dist_rows)
    if feature_day.height == 0:
        raise ValueError(f"no group had settled backfill for {day} — nothing validated")

    if cell_blocks:
        validation_store.write_cell(val_root, day, pl.concat(cell_blocks))
    non_empty_exc = [block for block in exception_blocks if block.height]
    if non_empty_exc:
        validation_store.write_exceptions(val_root, day, pl.concat(non_empty_exc))
    validation_store.upsert_feature_day(val_root, feature_day)
    trust = recompute_trust(validation_store.read_feature_day(val_root))
    validation_store.write_trust(val_root, trust)
    return trust


def _assemble_feature_day(tolerance_blocks: list[pl.DataFrame], dist_rows: list[dict]) -> pl.DataFrame:
    """Unify tolerance feature_day frames and distributional dict rows into one durable frame."""
    columns = ["version", "feature", "day", "method", "nan_policy", "n_compared", "n_match",
               "n_mismatch", "n_extra_live", "n_missing_live", "value_rate", "coverage_rate", "worst_abs_err"]
    blocks = [block.select(columns) for block in tolerance_blocks if block.height]
    if dist_rows:
        blocks.append(pl.DataFrame(dist_rows).select(columns))
    return pl.concat(blocks) if blocks else pl.DataFrame()


def main() -> None:
    args = sys.argv[1:]
    allow_today = "--allow-today" in args
    args = [arg for arg in args if arg != "--allow-today"]
    if len(args) != 3:
        raise SystemExit(
            "usage: python -m quantlib.features.validate <YYYY-MM-DD> <feature_store_root> <validation_root> [--allow-today]"
        )
    day, feature_root, val_root = args
    trust = validate(feature_root, day, val_root, allow_today=allow_today)
    pl.Config.set_tbl_rows(60)
    print(f"=== Validation trust registration after {day} ===")
    print(trust.select("feature", "method", "status", "value_grade", "coverage_grade",
                       "n_days_validated", "lifetime_value_rate"))
    divergent = trust.filter(pl.col("status") == "divergent")
    if divergent.height:
        print(f"\nDIVERGENT features (below floor — NOT trustworthy): {divergent['feature'].to_list()}")
        raise SystemExit(1)  # loud: a divergent feature must fail a wrapping certify gate
    print("\nNo divergent features.")


if __name__ == "__main__":
    main()
