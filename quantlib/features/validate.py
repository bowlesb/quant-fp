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
from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from quantlib.features import store, validation_db, validation_store
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
        pl.col("lifetime_value_rate")
        .map_elements(grade_for, return_dtype=pl.String, skip_nulls=False)
        .alias("value_grade"),
        pl.col("lifetime_coverage_rate")
        .map_elements(grade_for, return_dtype=pl.String, skip_nulls=False)
        .alias("coverage_grade"),
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


def _scope_tiers(tiers: pl.DataFrame, scope_symbols: list[str] | None) -> pl.DataFrame:
    """Restrict the day's tier membership to ``scope_symbols`` when a symbol scope is requested, so the
    store reads only load those symbols (the filter is pushed down into ``store.get_features`` ->
    ``_scan_source``'s lazy ``is_in``, never loading the full ~11k-symbol root then filtering in memory).
    ``None`` keeps the full-universe behavior. Pinning the scope to the day's tier membership preserves
    the universe-pinning contract: a requested symbol absent from the day's universe is dropped, never
    compared against a tier it does not belong to."""
    if scope_symbols is None:
        return tiers
    requested = set(scope_symbols)
    scoped = tiers.filter(pl.col("symbol").is_in(list(requested)))
    if scoped.height == 0:
        raise ValueError(
            f"none of the requested symbols {sorted(requested)} are in the day's universe membership — "
            f"nothing to validate (check the symbols are in universe_membership for this day)"
        )
    return scoped


@dataclass
class CompareResult:
    """The three durable frames a comparison pass produces, BEFORE persistence: the per-(version,
    feature, day) trust rollup, the per-(feature, symbol) cell rollup, and the diverging-cell
    exceptions. A split sweep (cross-sectional groups full-universe + per-symbol groups gradable-set)
    produces one of these per scope and CONCATENATES them, so each feature is graded against the
    backfill scope that makes its comparison fair — then persisted once."""

    feature_day: pl.DataFrame
    cell: pl.DataFrame
    exceptions: pl.DataFrame


def scoped_tiers(day: str, symbols: list[str] | None = None) -> tuple[list[str], pl.DataFrame]:
    """The day's universe membership pinned to ``symbols`` (None = full universe), returning
    ``(scope_symbols, tiers)`` for ``compare_groups``. Centralizes the membership pin both the sweep
    (cross-sectional full-universe + gradable-set scopes) and ``validate`` use."""
    tiers = load_tiers(day)
    if tiers.height == 0:
        raise ValueError(f"no universe membership for {day} — cannot validate (build_universe must have run)")
    tiers = _scope_tiers(tiers, symbols)
    return tiers["symbol"].to_list(), tiers


def compare_groups(
    feature_root: str,
    day: str,
    scope_symbols: list[str],
    tiers: pl.DataFrame,
    groups: list[str] | None = None,
) -> CompareResult:
    """Compare stream vs backfill for ``groups`` (None = all registered groups) over ``scope_symbols``,
    returning the durable rollup/cell/exceptions frames WITHOUT persisting them.

    Splitting compute from persistence lets the sweep grade cross-sectional (universe-reduce) groups
    against a FULL-UNIVERSE backfill while grading per-symbol/tick groups against the gradable set, then
    union the two results into one set of writes. ``tiers`` is the day's universe membership (already
    scoped to ``scope_symbols`` by the caller); both sides are pinned to it."""
    specs = {spec.name: spec for _, spec in REGISTRY.feature_specs()}
    version_of = {spec.name: group.version for group, spec in REGISTRY.feature_specs()}
    nan_policy_of = {name: spec.nan_policy for name, spec in specs.items()}
    start = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), tzinfo=timezone.utc)
    end = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 23, 59, 59, tzinfo=timezone.utc)

    feature_day_rows: list[pl.DataFrame] = []
    dist_rows: list[dict] = []
    cell_blocks: list[pl.DataFrame] = []
    exception_blocks: list[pl.DataFrame] = []
    for group in REGISTRY.groups():
        if groups is not None and group.name not in groups:
            continue
        feats = [spec.name for spec in group.declare()]
        backfill = store.get_features(feats, scope_symbols, start, end, feature_root, source="backfill")
        if backfill.height == 0:
            continue  # no settled backfill for this group/day -> unvalidated, skip (per-group settled check)
        live = store.get_features(feats, scope_symbols, start, end, feature_root, source="stream")
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
    cell = pl.concat(cell_blocks) if cell_blocks else pl.DataFrame()
    non_empty_exc = [block for block in exception_blocks if block.height]
    exceptions = pl.concat(non_empty_exc) if non_empty_exc else pl.DataFrame()
    return CompareResult(feature_day=feature_day, cell=cell, exceptions=exceptions)


def merge_results(results: list[CompareResult]) -> CompareResult:
    """Concatenate disjoint-group comparison results into one. The sweep grades cross-sectional groups in
    one pass (full-universe scope) and the rest in another (gradable-set scope); the group sets are
    disjoint, so concatenating their frames yields the complete day with no feature double-counted."""
    non_empty = lambda frames: [frame for frame in frames if frame.height]  # noqa: E731
    feature_day = non_empty([result.feature_day for result in results])
    cell = non_empty([result.cell for result in results])
    exceptions = non_empty([result.exceptions for result in results])
    return CompareResult(
        feature_day=pl.concat(feature_day) if feature_day else pl.DataFrame(),
        cell=pl.concat(cell) if cell else pl.DataFrame(),
        exceptions=pl.concat(exceptions) if exceptions else pl.DataFrame(),
    )


def persist_validation(
    val_root: str, day: str, result: CompareResult
) -> pl.DataFrame:
    """Persist a (possibly merged) comparison result: write the cell + exceptions layers, upsert the
    per-feature-day rollup, recompute + write trust, and write the canonical Postgres record. Returns the
    recomputed trust frame. ``write_cell``/``upsert_feature_day`` are whole-day replaces, so the caller
    must pass the COMPLETE day's result (the union of every scope) in ONE call."""
    if result.feature_day.height == 0:
        raise ValueError(f"no group had settled backfill for {day} — nothing validated")
    if result.cell.height:
        validation_store.write_cell(val_root, day, result.cell)
    if result.exceptions.height:
        validation_store.write_exceptions(val_root, day, result.exceptions)
    validation_store.upsert_feature_day(val_root, result.feature_day)  # parquet: cross-day accumulation source
    trust = recompute_trust(validation_store.read_feature_day(val_root))
    validation_store.write_trust(val_root, trust)
    validation_db.write_validation(result.feature_day, trust, result.exceptions, day)  # Postgres canonical record
    return trust


def validate(
    feature_root: str,
    day: str,
    val_root: str,
    allow_today: bool = False,
    symbols: list[str] | None = None,
    groups: list[str] | None = None,
) -> pl.DataFrame:
    """Validate one settled day's stored stream vs backfill, write the ledger, recompute trust, return it.

    ``symbols`` restricts the comparison to a small SCOPE of symbols (e.g. ~10 liquid names) — the
    symbol filter is pushed into the store reads, so only those partitions are loaded (the full-root
    load is the OOM the scope avoids). ``None`` keeps the full-universe run. A scoped run still PINS to
    the day's tier membership, just over the scoped subset. ``groups`` restricts the comparison to a
    subset of registered groups (None = all)."""
    assert_settled(day, allow_today)
    scope_symbols, tiers = scoped_tiers(day, symbols)  # PIN both sides to the day's universe membership
    result = compare_groups(feature_root, day, scope_symbols, tiers, groups=groups)
    return persist_validation(val_root, day, result)


_COUNT_COLUMNS = ("n_compared", "n_match", "n_mismatch", "n_extra_live", "n_missing_live")


def _assemble_feature_day(tolerance_blocks: list[pl.DataFrame], dist_rows: list[dict]) -> pl.DataFrame:
    """Unify tolerance feature_day frames and distributional dict rows into one durable frame.

    The tolerance path derives its counts from polars aggregations (UInt32) while the distributional
    path builds them from Python ints (Int64); cast the count columns to a common Int64 so the two
    sources vstack cleanly (the DB column is BIGINT either way)."""
    columns = ["version", "feature", "day", "method", "nan_policy", "n_compared", "n_match",
               "n_mismatch", "n_extra_live", "n_missing_live", "value_rate", "coverage_rate", "worst_abs_err"]
    count_casts = [pl.col(name).cast(pl.Int64) for name in _COUNT_COLUMNS]
    blocks = [block.select(columns).with_columns(count_casts) for block in tolerance_blocks if block.height]
    if dist_rows:
        blocks.append(pl.DataFrame(dist_rows).select(columns).with_columns(count_casts))
    return pl.concat(blocks) if blocks else pl.DataFrame()


def _parse_symbols(args: list[str]) -> tuple[list[str], list[str] | None]:
    """Pull an optional ``--symbols AAPL,MSFT,...`` scope out of the positional args."""
    symbols: list[str] | None = None
    rest: list[str] = []
    iterator = iter(args)
    for arg in iterator:
        if arg == "--symbols":
            symbols = [token.strip() for token in next(iterator).split(",") if token.strip()]
        elif arg.startswith("--symbols="):
            symbols = [token.strip() for token in arg.removeprefix("--symbols=").split(",") if token.strip()]
        else:
            rest.append(arg)
    return rest, symbols


def main() -> None:
    args = sys.argv[1:]
    allow_today = "--allow-today" in args
    args = [arg for arg in args if arg != "--allow-today"]
    args, symbols = _parse_symbols(args)
    if len(args) != 3:
        raise SystemExit(
            "usage: python -m quantlib.features.validate <YYYY-MM-DD> <feature_store_root> <validation_root> "
            "[--allow-today] [--symbols AAPL,MSFT,...]"
        )
    day, feature_root, val_root = args
    trust = validate(feature_root, day, val_root, allow_today=allow_today, symbols=symbols)
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
