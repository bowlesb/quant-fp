"""The CRYPTO parity sweep — prove the crypto live emit (source=stream) == its batch recompute
(source=backfill) for the universal groups, on a crypto store, writing crypto-namespaced trust grades.

This is the off-hours rehearsal of the equity ``validation_sweep`` (docs/CRYPTO_E2E.md). It reuses the
EQUITY parity math and grading logic unchanged — only the equity-specific seams are replaced:

  * **No raw tape.** Crypto has no ``/store/raw`` and no equity-style backfill acquisition. The backfill side
    is RECOMPUTED from the SAME ``minute_agg`` (+ ``trades``) inputs the live feed delivered, persisted by
    ``crypto_input_store`` — a genuine live-emit-vs-batch-recompute parity test (docs/CRYPTO_E2E.md §3).
  * **No RTH window.** Crypto trades 24/7, so the equity ``rth_mask`` (09:30-16:00 ET) would drop every crypto
    minute. We compare over ALL minutes of the UTC day instead.
  * **No NYSE calendar / settle gate / market-ticker pin.** Crypto is 24/7 and has no SPY/QQQ reference; the
    UTC day is the unit, the day is gradable as soon as its inputs are persisted, and there is no market pin.
  * **Crypto tiers.** No ADV$ universe membership; all crypto symbols sit at tier 1 (they are all liquid),
    enough for the tier-keyed grouping the parity rollup uses.
  * **Crypto trust ledger.** Grants go to ``crypto_feature_trust`` (asset_class='crypto'), never the equity
    ``feature_trust`` — so crypto and equity trust can never collide (docs/CRYPTO_E2E.md §1).

The parity VERDICT itself (``cell_verdict`` / the per-type tolerances) and the contamination-aware grading
(``clean_feature_day`` over clean symbols, ``earned_features`` at the trust policy's min_pass_rate) are the
SAME code the equity sweep runs — a crypto match == an equity match by construction.

Usage:
  python -m quantlib.features.crypto_validation_sweep <YYYY-MM-DD> <crypto_root> <val_root>
  (day defaults to the latest day with persisted crypto inputs.)
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
from dataclasses import replace

import polars as pl

from quantlib.features import crypto_input_store, crypto_trust, store, trust_binary
from quantlib.features.base import KEY_COLUMNS, FeatureType
from quantlib.features.cleanliness import clean_symbols, symbol_day_cleanliness
from quantlib.features.compare import runnable
from quantlib.features.crypto_capture import EXCLUDED_GROUPS
from quantlib.features.materialize import _write_all
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_lifecycle import clean_feature_day
from quantlib.features.validate import (
    CompareResult,
    _assemble_feature_day,
    _cell_rollup,
    _exceptions,
    _feature_day_tolerance,
    _long_verdicts,
)

logger = logging.getLogger("crypto_validation_sweep")

# A bar feature non-null at every minute a crypto bar printed (BOTH sides) — the minute-coverage signal the
# cleanliness heuristic reads. ret_1m is the universal price-return feature, present on crypto.
COVERAGE_FEATURES = ["ret_1m"]
# Crypto is 24/7 and the symbol set is tiny + perpetually liquid, so the equity MIN_CLEAN_SYMBOLS=20 floor
# (a contamination guard tuned for an ~11k-name universe) does not apply; a single clean crypto pair is a
# valid clean comparison for the rehearsal. The floor is 1 (need at least one clean symbol to grade).
MIN_CLEAN_SYMBOLS = 1


def _day_bounds(day: str) -> tuple[dt.datetime, dt.datetime]:
    year, month, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
    start = dt.datetime(year, month, dom, tzinfo=dt.timezone.utc)
    end = dt.datetime(year, month, dom, 23, 59, 59, tzinfo=dt.timezone.utc)
    return start, end


def crypto_tiers(symbols: list[str]) -> pl.DataFrame:
    """The crypto tier membership: every crypto symbol at tier 1 (all liquid). The parity rollup groups by
    tier; crypto has no ADV$ ranking, so a single tier is the honest mapping (and the distributional
    features' per-tier scan still works — tiers 2/3 are simply empty)."""
    return pl.DataFrame(
        {"symbol": symbols, "tier": [1] * len(symbols)}, schema={"symbol": pl.String, "tier": pl.Int32}
    )


def materialize_crypto_backfill(crypto_root: str, day: str) -> int:
    """Recompute the crypto ``source=backfill`` side from the persisted ``minute_agg`` (+ ``trades``) inputs,
    via the IDENTICAL batch ``_write_all`` path the equity backfill uses. Returns the symbol count, or 0 if
    no inputs were persisted for the day (nothing to recompute)."""
    minute_agg = crypto_input_store.load_input(crypto_root, "minute_agg", day)
    if minute_agg.height == 0:
        return 0
    frames: dict[str, pl.DataFrame] = {"minute_agg": minute_agg}
    trades = crypto_input_store.load_input(crypto_root, "trades", day)
    if trades.height:
        frames["trades"] = trades
    # No daily/reference/filings (crypto passes none live either). Scope to the runnable groups MINUS the
    # SPY-relative EXCLUDED_GROUPS so the backfill set matches the live crypto feature set exactly (the live
    # crypto path excludes the same groups). ``_write_all`` has only_groups (an allow-list), so express the
    # exclusion as "every runnable group except the excluded ones".
    only = [group.name for group in runnable(frames) if group.name not in set(EXCLUDED_GROUPS)]
    return _write_all(crypto_root, day, "backfill", frames, only_groups=only)


def crypto_day_cleanliness(crypto_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """Per-(symbol) CLEAN/contaminated verdict for the crypto day from bar-feature minute coverage — the
    24/7 analogue of the equity ``day_cleanliness`` (no ``rth_mask``: crypto trades every minute)."""
    start, end = _day_bounds(day)
    stream = store.get_features(COVERAGE_FEATURES, symbols, start, end, crypto_root, source="stream")
    backfill = store.get_features(COVERAGE_FEATURES, symbols, start, end, crypto_root, source="backfill")
    if backfill.height == 0:
        return pl.DataFrame()
    if stream.height == 0:
        stream = backfill.clear()
    joined = stream.join(backfill, on=["symbol", "minute"], how="full", suffix="_bk", coalesce=True)
    return symbol_day_cleanliness(joined)


def compare_crypto_groups(
    crypto_root: str,
    day: str,
    scope_symbols: list[str],
    tiers: pl.DataFrame,
    groups: list[str],
    tolerance_of: dict[str, float],
) -> CompareResult:
    """Compare crypto stream vs backfill for ``groups`` over ``scope_symbols``, returning the rollup/cell/
    exceptions WITHOUT persisting. Reuses the equity parity primitives (``_long_verdicts`` / ``_cell_rollup``
    / ``_exceptions`` / ``_feature_day_tolerance`` — same ``cell_verdict``, same tolerances) but over ALL
    minutes of the UTC day (NO ``rth_mask``) and against the crypto tier frame."""
    specs = {spec.name: spec for _, spec in REGISTRY.feature_specs()}
    specs = {
        name: replace(spec, tolerance=tolerance_of.get(name, spec.tolerance)) for name, spec in specs.items()
    }
    version_of = {spec.name: group.version for group, spec in REGISTRY.feature_specs()}
    nan_policy_of = {name: spec.nan_policy for name, spec in specs.items()}
    start, end = _day_bounds(day)

    feature_day_rows: list[pl.DataFrame] = []
    cell_blocks: list[pl.DataFrame] = []
    exception_blocks: list[pl.DataFrame] = []
    for group in REGISTRY.groups():
        if group.name not in groups:
            continue
        feats = [spec.name for spec in group.declare()]
        backfill = store.get_features(feats, scope_symbols, start, end, crypto_root, source="backfill")
        if backfill.height == 0:
            continue
        live = store.get_features(feats, scope_symbols, start, end, crypto_root, source="stream")
        if live.height == 0:
            live = backfill.select(KEY_COLUMNS).clear()
        joined = (
            live.join(backfill, on=list(KEY_COLUMNS), how="full", suffix="_bk", coalesce=True)
            .join(tiers, on="symbol", how="left")
            .with_columns(pl.col("tier").fill_null(1))
        )
        # Tolerance features only — crypto rehearsal grades cell-for-cell parity (the distributional
        # tick-order features are validated by the equity sweep; not needed for the crypto trust slice).
        tol_feats = [f for f in feats if specs[f].parity_method != "distributional"]
        if not tol_feats:
            continue
        long = _long_verdicts(joined, tol_feats, specs)
        if long.height == 0:
            continue
        cell = _cell_rollup(long)
        cell_blocks.append(cell)
        exception_blocks.append(_exceptions(long, day))
        feature_day_rows.append(_feature_day_tolerance(cell, version_of, nan_policy_of, day))

    feature_day = _assemble_feature_day(feature_day_rows, [])
    cell = pl.concat(cell_blocks) if cell_blocks else pl.DataFrame()
    non_empty_exc = [block for block in exception_blocks if block.height]
    exceptions = pl.concat(non_empty_exc) if non_empty_exc else pl.DataFrame()
    return CompareResult(feature_day=feature_day, cell=cell, exceptions=exceptions)


def crypto_groups(crypto_root: str, day: str) -> list[str]:
    """The universal groups the crypto sweep grades for the day: what ``runnable`` self-selects from the
    persisted crypto inputs, minus the SPY-relative EXCLUDED_GROUPS and the cross-sectional reduce groups
    (the first slice grades per-symbol/tick groups; the cross-sectional grade is a documented next step)."""
    minute_agg = crypto_input_store.load_input(crypto_root, "minute_agg", day)
    frames: dict[str, pl.DataFrame] = {"minute_agg": minute_agg}
    trades = crypto_input_store.load_input(crypto_root, "trades", day)
    if trades.height:
        frames["trades"] = trades
    return [
        group.name
        for group in runnable(frames)
        if group.name not in set(EXCLUDED_GROUPS) and group.type != FeatureType.CROSS_SECTIONAL
    ]


def sweep_crypto_day(crypto_root: str, val_root: str, day: str) -> dict[str, object]:
    """Run the crypto parity sweep for one UTC day and return a summary dict. Recomputes the backfill side
    from persisted inputs, compares stream vs backfill, grades cleanliness, and writes the crypto trust
    grants. ``val_root`` is accepted for parity with the equity signature (reserved for a future crypto
    validation-store; this slice's durable record is the crypto trust ledger)."""
    materialized = materialize_crypto_backfill(crypto_root, day)
    if materialized == 0:
        return {"day": day, "note": "no persisted crypto inputs for the day — nothing to recompute/sweep"}

    discovered = store.stream_symbols_on(crypto_root, day)
    if not discovered:
        return {"day": day, "note": "no source=stream crypto symbols — nothing to sweep"}

    cleanliness = crypto_day_cleanliness(crypto_root, day, discovered)
    clean_count = int(cleanliness["is_clean"].sum()) if cleanliness.height else 0
    gradable = clean_symbols(cleanliness)
    if clean_count < MIN_CLEAN_SYMBOLS:
        return {
            "day": day,
            "discovered": len(discovered),
            "materialized": materialized,
            "clean_symbols": clean_count,
            "features_graded": 0,
            "note": f"clean breadth {clean_count} < MIN_CLEAN_SYMBOLS {MIN_CLEAN_SYMBOLS} — nothing graded",
        }

    groups = crypto_groups(crypto_root, day)
    tiers = crypto_tiers(gradable)
    tolerance_of = trust_binary.cell_tolerance_map()
    result = compare_crypto_groups(crypto_root, day, gradable, tiers, groups, tolerance_of)

    clean_today = clean_feature_day(result.cell, gradable, day)
    earned = trust_binary.earned_features(clean_today, trust_binary.feature_policy_map())
    grant_counts = crypto_trust.write_crypto_grants(earned, clean_today, day)

    graded = result.feature_day.height
    passed = int(clean_today.filter(pl.col("passed")).height) if clean_today.height else 0
    return {
        "day": day,
        "discovered": len(discovered),
        "materialized": materialized,
        "clean_symbols": clean_count,
        "groups_graded": len(groups),
        "features_graded": graded,
        "features_passed_clean": passed,
        "newly_trusted_crypto": grant_counts["earned_trusted"],
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:]
    day: str | None = None
    if args and len(args[0]) == 10 and args[0][4] == "-":
        day = args.pop(0)
    if len(args) < 2:
        raise SystemExit(
            "usage: python -m quantlib.features.crypto_validation_sweep [YYYY-MM-DD] <crypto_root> <val_root>"
        )
    crypto_root, val_root = args[0], args[1]
    if day is None:
        days = crypto_input_store.input_days(crypto_root)
        if not days:
            print(
                f"no persisted crypto inputs under {crypto_root} — run crypto-capture with "
                f"{crypto_input_store.PERSIST_ENV}=1 first"
            )
            return
        day = days[-1]
    summary = sweep_crypto_day(crypto_root, val_root, day)
    print(f"=== Crypto parity sweep summary for {day} ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
