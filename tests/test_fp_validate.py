"""The validation job's trust math — cell rollup, per-day aggregation, and the per-feature trust grade.

Proves the ledger turns stored stream-vs-backfill cells into the right durable verdicts and the right
trust status (validating / certified / divergent), the registration the platform gates training on.
DB/store I/O (load_tiers, get_features) is exercised by the integration path; here we pin the pure logic.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import validation_db, validation_store
from quantlib.features.base import FeatureSpec
from quantlib.features.validate import (
    _assemble_feature_day,
    _cell_rollup,
    _exceptions,
    _feature_day_tolerance,
    _long_verdicts,
    assert_settled,
    grade_for,
    recompute_trust,
)

DESC = "x" * 40
SPECS = {"ret_5m": FeatureSpec(name="ret_5m", description=DESC, dtype="Float64", tolerance=1e-6)}
VERSION_OF = {"ret_5m": "v1.0.0"}
NAN_POLICY_OF = {"ret_5m": "none"}
M0 = dt.datetime(2026, 6, 12, 14, 0, tzinfo=dt.timezone.utc)
M1 = dt.datetime(2026, 6, 12, 14, 1, tzinfo=dt.timezone.utc)

FEATURE_DAY_COLS = ["version", "feature", "day", "method", "nan_policy", "n_compared", "n_match",
                    "n_mismatch", "n_extra_live", "n_missing_live", "value_rate", "coverage_rate", "worst_abs_err"]


def _joined() -> pl.DataFrame:
    """A 4-cell joined frame exercising all verdicts: match, mismatch, extra_live, missing_live."""
    return pl.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "minute": [M0, M1, M0, M1],
            "tier": [1, 1, 2, 2],
            "ret_5m": [1.0, 1.0, 2.0, None],
            "ret_5m_bk": [1.0, 1.5, None, 9.0],
        }
    )


def test_grade_for_thresholds() -> None:
    assert grade_for(1.0) == "A"
    assert grade_for(0.9995) == "B"
    assert grade_for(0.995) == "C"
    assert grade_for(0.5) == "F"
    assert grade_for(None) == "U"


def test_assert_settled_rejects_today_and_future() -> None:
    today = dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()
    with pytest.raises(ValueError, match="not settled"):
        assert_settled(today, allow_today=False)
    assert_settled("2020-01-01", allow_today=False)  # a settled past day is fine
    assert_settled(today, allow_today=True)  # override for closed-session tests


def test_cell_rollup_counts_every_verdict() -> None:
    long = _long_verdicts(_joined(), ["ret_5m"], SPECS)
    rollup = _cell_rollup(long).sort("symbol")
    by_symbol = {row["symbol"]: row for row in rollup.to_dicts()}
    assert (by_symbol["A"]["n_match"], by_symbol["A"]["n_mismatch"]) == (1, 1)
    assert (by_symbol["B"]["n_extra_live"], by_symbol["B"]["n_missing_live"]) == (1, 1)


def test_feature_day_rates() -> None:
    long = _long_verdicts(_joined(), ["ret_5m"], SPECS)
    feature_day = _feature_day_tolerance(_cell_rollup(long), VERSION_OF, NAN_POLICY_OF, "2026-06-12").to_dicts()[0]
    assert feature_day["n_compared"] == 2  # match + mismatch
    assert feature_day["value_rate"] == pytest.approx(0.5)  # 1 of 2 compared cells agree
    assert feature_day["coverage_rate"] == pytest.approx(2 / 3)  # 2 compared / (2 compared + 1 missing)
    assert feature_day["version"] == "v1.0.0"


def _feature_day(day: str, n_compared: int, n_match: int, n_missing: int = 0) -> dict:
    return {
        "version": "v1.0.0", "feature": "ret_5m", "day": day, "method": "tolerance", "nan_policy": "none",
        "n_compared": n_compared, "n_match": n_match, "n_mismatch": n_compared - n_match,
        "n_extra_live": 0, "n_missing_live": n_missing,
        "value_rate": (n_match / n_compared) if n_compared else None,
        "coverage_rate": (n_compared / (n_compared + n_missing)) if (n_compared + n_missing) else None,
        "worst_abs_err": 0.0,
    }


def test_recompute_trust_certifies_after_min_days() -> None:
    rows = pl.DataFrame([_feature_day(f"2026-06-{day:02d}", 1000, 1000) for day in range(6, 12)]).select(FEATURE_DAY_COLS)
    trust = recompute_trust(rows).to_dicts()[0]
    assert trust["n_days_validated"] == 6
    assert trust["lifetime_value_rate"] == pytest.approx(1.0)
    assert trust["value_grade"] == "A"
    assert trust["status"] == "certified"


def test_recompute_trust_validating_below_min_days() -> None:
    rows = pl.DataFrame([_feature_day(f"2026-06-{day:02d}", 1000, 1000) for day in range(6, 9)]).select(FEATURE_DAY_COLS)
    trust = recompute_trust(rows).to_dicts()[0]
    assert trust["n_days_validated"] == 3
    assert trust["status"] == "validating"  # not enough history to certify yet


def test_recompute_trust_divergent_when_last_day_below_floor() -> None:
    good = [_feature_day(f"2026-06-{day:02d}", 1000, 1000) for day in range(6, 11)]
    bad = [_feature_day("2026-06-11", 1000, 900)]  # last day 90% < HARD_FLOOR 95%
    rows = pl.DataFrame(good + bad).select(FEATURE_DAY_COLS)
    trust = recompute_trust(rows).to_dicts()[0]
    assert trust["status"] == "divergent"  # a single broken day flips it loudly


def test_recompute_trust_grades_null_rate_as_unvalidated() -> None:
    """A feature with zero compared cells (all missing_live) has a null lifetime rate — its grade must be
    'U' (unvalidated), never null. Polars map_elements skips nulls by default, which would emit a null
    grade and violate feature_trust.value_grade NOT NULL; skip_nulls=False feeds None through grade_for."""
    rows = pl.DataFrame([_feature_day("2026-06-10", 0, 0, n_missing=0)]).select(FEATURE_DAY_COLS)
    trust = recompute_trust(rows).to_dicts()[0]
    assert trust["lifetime_value_rate"] is None
    assert trust["lifetime_coverage_rate"] is None
    assert trust["value_grade"] == "U"  # not null — DB requires NOT NULL
    assert trust["coverage_grade"] == "U"


def _trust_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "feature": ["good", "low_value", "low_cover", "still_validating", "diverged"],
            "value_grade": ["A", "F", "A", "A", "A"],
            "coverage_grade": ["A", "A", "F", "A", "A"],
            "status": ["certified", "certified", "certified", "validating", "divergent"],
        }
    )


def test_certified_features_gates_on_grade_and_status(tmp_path) -> None:
    validation_store.write_trust(tmp_path, _trust_frame())
    certified = validation_store.certified_features(tmp_path, min_value_grade="B", min_coverage_grade="B")
    assert certified == {"good"}  # the others fail on value, coverage, status(validating), or status(divergent)


def test_untrusted_among_flags_what_training_must_refuse(tmp_path) -> None:
    validation_store.write_trust(tmp_path, _trust_frame())
    requested = ["good", "diverged", "never_seen"]
    assert validation_store.untrusted_among(requested, tmp_path) == {"diverged", "never_seen"}


def test_certified_features_empty_when_unvalidated(tmp_path) -> None:
    assert validation_store.certified_features(tmp_path) == set()  # nothing certified until validated


def test_db_rows_day_adds_all_tier_and_orders_columns() -> None:
    long = _long_verdicts(_joined(), ["ret_5m"], SPECS)
    feature_day = _feature_day_tolerance(_cell_rollup(long), VERSION_OF, NAN_POLICY_OF, "2026-06-12")
    rows = validation_db._rows_day(feature_day)
    assert len(rows) == 1
    # column order: feature, version, day, tier(=0), method, n_compared, n_match, ...
    assert rows[0][:6] == ("ret_5m", "v1.0.0", "2026-06-12", 0, "tolerance", 2)


def test_db_rows_exceptions_map_minute_to_ts() -> None:
    long = _long_verdicts(_joined(), ["ret_5m"], SPECS)
    exceptions = _exceptions(long, "2026-06-12")
    rows = validation_db._rows_exceptions(exceptions)
    # A (mismatch) and B (extra_live) -> 2 exception rows; ts is the cell minute, in column 3.
    assert len(rows) == 2
    assert all(row[2] in (M0, M1) for row in rows)


def test_db_rows_empty_frames_yield_no_rows() -> None:
    assert validation_db._rows_day(pl.DataFrame()) == []
    assert validation_db._rows_trust(pl.DataFrame()) == []
    assert validation_db._rows_exceptions(pl.DataFrame()) == []


def test_assemble_feature_day_mixes_tolerance_and_distributional_dtypes() -> None:
    """A tolerance block (counts UInt32 from polars aggs) and a distributional dict row (counts Python
    int -> Int64) must vstack cleanly — the order-flow groups (tick_runlength / microstructure_burst,
    distributional) only get a backfill side now, so the two count dtypes meet for the first time."""
    tolerance = pl.DataFrame(
        {
            "version": ["v1"], "feature": ["ret_5m"], "day": ["2026-06-12"], "method": ["tolerance"],
            "nan_policy": ["none"],
            "n_compared": pl.Series([10], dtype=pl.UInt32),
            "n_match": pl.Series([9], dtype=pl.UInt32),
            "n_mismatch": pl.Series([1], dtype=pl.UInt32),
            "n_extra_live": pl.Series([0], dtype=pl.UInt32),
            "n_missing_live": pl.Series([0], dtype=pl.UInt32),
            "value_rate": [0.9], "coverage_rate": [1.0], "worst_abs_err": [0.01],
        }
    )
    dist_row = {
        "version": "v1", "feature": "tick_run_up_1m", "day": "2026-06-12", "method": "distributional",
        "nan_policy": "sparse", "n_match": 5, "n_mismatch": 0, "n_extra_live": 0, "n_missing_live": 0,
        "n_compared": 5, "value_rate": 1.0, "coverage_rate": 1.0, "worst_abs_err": None,
    }
    out = _assemble_feature_day([tolerance], [dist_row])
    assert out.height == 2
    assert out["n_compared"].dtype == pl.Int64
    assert set(out["feature"].to_list()) == {"ret_5m", "tick_run_up_1m"}


def test_upsert_feature_day_is_idempotent(tmp_path) -> None:
    rows = pl.DataFrame([_feature_day("2026-06-10", 1000, 1000)]).select(FEATURE_DAY_COLS)
    validation_store.upsert_feature_day(tmp_path, rows)
    validation_store.upsert_feature_day(tmp_path, rows)  # re-validate the SAME day
    stored = validation_store.read_feature_day(tmp_path)
    assert stored.height == 1  # replaced, not double-counted
    assert stored["n_match"][0] == 1000
