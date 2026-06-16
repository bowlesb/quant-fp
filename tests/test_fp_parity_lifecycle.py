"""The contamination-aware parity LIFECYCLE — cleanliness heuristic, trust state machine, defect backlog.

Network-free: every assertion is over pure polars frames / row-builders (no DB, no store I/O). Proves the
three correctness points of docs/PARITY_LIFECYCLE.md:
  * a gappy/low-coverage stream symbol-day is flagged CONTAMINATED; a full one CLEAN;
  * PENDING -> VALIDATED needs N clean days, and DIVERGENT comes ONLY from clean-day failures (a feature
    that fails only on contaminated days is NOT condemned);
  * the defect backlog upsert builds one row per DIVERGENT feature with exemplar diverging cells.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features.cleanliness import (
    MAX_GAP_MINUTES,
    MIN_COVERAGE_FRAC,
    clean_symbols,
    symbol_day_cleanliness,
)
from quantlib.features.trust_lifecycle import (
    MIN_CLEAN_DAYS,
    STATE_DIVERGENT,
    STATE_PENDING,
    STATE_RETIRED,
    STATE_VALIDATED,
    clean_feature_day,
    defect_rows,
    lifecycle_state,
)

OPEN_ET = dt.datetime(2026, 6, 12, 13, 30, tzinfo=dt.timezone.utc)  # 09:30 ET (June -> EDT, UTC-4)


def _minute(offset: int) -> dt.datetime:
    return OPEN_ET + dt.timedelta(minutes=offset)


def _coverage_frame(symbol: str, present_offsets: list[int], back_offsets: list[int]) -> pl.DataFrame:
    """A joined live+backfill frame: a feature value is non-null at the minutes the side 'has'."""
    offsets = sorted(set(present_offsets) | set(back_offsets))
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(offsets),
            "minute": [_minute(off) for off in offsets],
            "ret_1m": [1.0 if off in present_offsets else None for off in offsets],
            "ret_1m_bk": [1.0 if off in back_offsets else None for off in offsets],
        }
    )


def test_full_session_symbol_is_clean() -> None:
    full = list(range(390))
    cleanliness = symbol_day_cleanliness(_coverage_frame("FULL", full, full))
    row = cleanliness.row(0, named=True)
    assert row["is_clean"] is True
    assert row["reason"] == "clean"
    assert row["coverage_frac"] == pytest.approx(1.0)
    assert row["max_gap_minutes"] == 1


def test_internal_gap_symbol_is_contaminated() -> None:
    """A capture restart leaves a hole > MAX_GAP_MINUTES even if total coverage looks high — contaminated."""
    back = list(range(390))
    # stream missing minutes 100..115 (a 16-min restart hole); coverage ~96% but the gap breaks windows.
    stream = [off for off in back if not (100 <= off < 116)]
    cleanliness = symbol_day_cleanliness(_coverage_frame("GAP", stream, back))
    row = cleanliness.row(0, named=True)
    assert row["max_gap_minutes"] > MAX_GAP_MINUTES
    assert row["is_clean"] is False
    assert row["reason"] == "internal_gap"


def test_low_coverage_symbol_is_contaminated() -> None:
    back = list(range(390))
    stream = list(range(200))  # only the morning captured -> ~51% coverage
    cleanliness = symbol_day_cleanliness(_coverage_frame("HALF", stream, back))
    row = cleanliness.row(0, named=True)
    assert row["coverage_frac"] < MIN_COVERAGE_FRAC
    assert row["is_clean"] is False
    assert row["reason"] == "low_coverage"


def test_sparse_but_complete_thin_name_is_clean() -> None:
    """A thin name that legitimately prints few bars is CLEAN: coverage is vs backfill-present minutes, not
    a flat 390, and its minutes are contiguous (no internal restart hole)."""
    back = list(range(0, 60))  # truth only had 60 contiguous minutes (a thin/halted name)
    stream = list(range(0, 60))
    cleanliness = symbol_day_cleanliness(_coverage_frame("THIN", stream, back))
    assert cleanliness.row(0, named=True)["is_clean"] is True


def test_clean_symbols_filters() -> None:
    full = list(range(390))
    half = list(range(150))
    frame = pl.concat([_coverage_frame("FULL", full, full), _coverage_frame("HALF", half, full)])
    cleanliness = symbol_day_cleanliness(frame)
    assert clean_symbols(cleanliness) == ["FULL"]


def _cell(feature: str, symbol: str, n_match: int, n_mismatch: int) -> dict:
    return {
        "feature": feature,
        "symbol": symbol,
        "tier": 1,
        "n_match": n_match,
        "n_mismatch": n_mismatch,
        "n_extra_live": 0,
        "n_missing_live": 0,
        "worst_abs_err": 0.0,
    }


def test_clean_feature_day_scopes_to_clean_symbols() -> None:
    """The clean-day grade aggregates ONLY clean symbols — a contaminated symbol's mismatches are excluded."""
    cell = pl.DataFrame(
        [
            _cell("feat", "CLEAN", n_match=390, n_mismatch=0),  # clean symbol: perfect parity
            _cell("feat", "DIRTY", n_match=100, n_mismatch=290),  # contaminated symbol: would fail if counted
        ]
    )
    rolled = clean_feature_day(cell, clean_symbols=["CLEAN"], day="2026-06-12")
    row = rolled.row(0, named=True)
    assert row["clean_compared"] == 390
    assert row["passed"] is True  # only CLEAN counted -> 100% -> passes


def test_clean_feature_day_fails_on_clean_symbol_divergence() -> None:
    cell = pl.DataFrame([_cell("feat", "CLEAN", n_match=100, n_mismatch=290)])
    rolled = clean_feature_day(cell, clean_symbols=["CLEAN"], day="2026-06-12")
    assert rolled.row(0, named=True)["passed"] is False  # a real compute bug on a clean day


def _clean_day_row(feature: str, day: str, passed: bool) -> dict:
    compared, match = 1000, (1000 if passed else 500)
    return {
        "feature": feature,
        "day": day,
        "clean_compared": compared,
        "clean_match": match,
        "clean_value_rate": match / compared,
        "passed": passed,
    }


def test_pending_below_min_clean_days() -> None:
    history = pl.DataFrame([_clean_day_row("feat", "2026-06-10", passed=True)])  # 1 clean day < MIN_CLEAN_DAYS
    states = lifecycle_state(history, retired=set())
    assert states.row(0, named=True)["lifecycle_state"] == STATE_PENDING


def test_validated_after_min_clean_days() -> None:
    history = pl.DataFrame(
        [_clean_day_row("feat", f"2026-06-{10 + i:02d}", passed=True) for i in range(MIN_CLEAN_DAYS)]
    )
    states = lifecycle_state(history, retired=set())
    row = states.row(0, named=True)
    assert row["clean_days"] == MIN_CLEAN_DAYS
    assert row["lifecycle_state"] == STATE_VALIDATED


def test_divergent_only_from_clean_day_failure() -> None:
    history = pl.DataFrame(
        [
            _clean_day_row("feat", "2026-06-10", passed=True),
            _clean_day_row("feat", "2026-06-11", passed=False),  # failed on a CLEAN day -> real bug
        ]
    )
    states = lifecycle_state(history, retired=set())
    assert states.row(0, named=True)["lifecycle_state"] == STATE_DIVERGENT


def test_contaminated_only_failure_does_not_condemn() -> None:
    """A feature that only ever fails on CONTAMINATED days never enters clean_history as a failure, so it
    is never DIVERGENT — the core contamination-awareness guarantee. Here both clean days passed."""
    history = pl.DataFrame(
        [
            _clean_day_row("feat", "2026-06-10", passed=True),
            _clean_day_row("feat", "2026-06-11", passed=True),
        ]
    )
    states = lifecycle_state(history, retired=set())
    assert states.row(0, named=True)["lifecycle_state"] == STATE_VALIDATED


def test_retired_is_terminal() -> None:
    history = pl.DataFrame(
        [_clean_day_row("feat", f"2026-06-{10 + i:02d}", passed=True) for i in range(MIN_CLEAN_DAYS)]
    )
    states = lifecycle_state(history, retired={"feat"})
    assert states.row(0, named=True)["lifecycle_state"] == STATE_RETIRED  # not VALIDATED despite clean days


def test_defect_rows_built_for_divergent_with_exemplars() -> None:
    history = pl.DataFrame(
        [
            _clean_day_row("feat", "2026-06-10", passed=False),
            _clean_day_row("feat", "2026-06-11", passed=False),
        ]
    )
    states = lifecycle_state(history, retired=set())
    exceptions = pl.DataFrame(
        {
            "feature": ["feat", "feat"],
            "symbol": ["AAA", "BBB"],
            "minute": [_minute(0), _minute(1)],
            "stream_value": [9.0, 8.0],
            "backfill_value": [1.0, 1.0],
            "rel_err": [8.0, 7.0],
        }
    )
    rows = defect_rows(states, history, exceptions, group_of={"feat": "grp"}, version_of={"feat": "1.0.0"})
    assert len(rows) == 1
    feature, version, group, first_seen, last_seen, days_failed, worst_rel, exemplars_json = rows[0]
    assert (feature, version, group) == ("feat", "1.0.0", "grp")
    assert (first_seen, last_seen, days_failed) == ("2026-06-10", "2026-06-11", 2)
    assert worst_rel == pytest.approx(8.0)
    assert '"symbol":' in exemplars_json and "AAA" in exemplars_json


def test_no_defect_when_no_divergent() -> None:
    history = pl.DataFrame(
        [_clean_day_row("feat", f"2026-06-{10 + i:02d}", passed=True) for i in range(MIN_CLEAN_DAYS)]
    )
    states = lifecycle_state(history, retired=set())
    assert defect_rows(states, history, pl.DataFrame(), group_of={"feat": "g"}, version_of={"feat": "1"}) == []
