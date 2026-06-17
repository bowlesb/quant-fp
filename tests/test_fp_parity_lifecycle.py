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
from quantlib.features import validation_sweep
from quantlib.features.validation_sweep import MARKET_TICKERS, MIN_CLEAN_SYMBOLS

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
    assert row["max_gap_minutes"] == 0  # no minute backfill had that the stream lacked


def test_internal_gap_symbol_is_contaminated() -> None:
    """A capture restart leaves a hole > MAX_GAP_MINUTES even if total coverage looks high — contaminated."""
    back = list(range(390))
    # stream missing minutes 100..115 (a 16-min restart hole); coverage ~96% but the gap breaks windows.
    stream = [off for off in back if not (100 <= off < 116)]
    cleanliness = symbol_day_cleanliness(_coverage_frame("GAP", stream, back))
    row = cleanliness.row(0, named=True)
    assert row["max_gap_minutes"] == 16  # the contiguous backfill-had-but-stream-missing run
    assert row["is_clean"] is False
    assert row["reason"] == "internal_gap"


def test_low_coverage_symbol_is_contaminated() -> None:
    back = list(range(390))
    stream = list(range(200))  # only the morning captured -> ~51% coverage
    cleanliness = symbol_day_cleanliness(_coverage_frame("HALF", stream, back))
    row = cleanliness.row(0, named=True)
    assert row["coverage_frac"] < MIN_COVERAGE_FRAC
    assert row["is_clean"] is False


def test_thin_session_name_is_not_a_fair_parity_test() -> None:
    """A name that printed only a few dozen minutes (a thin/halted/illiquid ticker) trivially passes the
    gap + coverage checks — it has no internal hole because it barely traded — but its windowed features
    are DEGENERATE (near-zero denominators), so its cells produce false parity failures. Below
    MIN_BACKFILL_MINUTES it is excluded from grading (reason thin_session), contributing no clean
    comparison rather than a false one. (Root-caused 2026-06-15: a capture-restart day whose only
    gap-surviving symbols were ~47-minute thin names -> 383 spurious DIVERGENT defects.)"""
    back = list(range(0, 60))  # truth only had 60 contiguous minutes -> below the 120-minute floor
    stream = list(range(0, 60))
    row = symbol_day_cleanliness(_coverage_frame("THIN", stream, back)).row(0, named=True)
    assert row["is_clean"] is False
    assert row["reason"] == "thin_session"


def test_substantial_partial_session_is_clean() -> None:
    """A name with a substantial-but-partial session (>= MIN_BACKFILL_MINUTES, fully captured, no internal
    gap) IS a fair parity test — the floor admits a real partial session, only excluding the degenerate
    thin tail."""
    back = list(range(0, 150))  # 150 contiguous minutes, above the 120-minute floor
    stream = list(range(0, 150))
    row = symbol_day_cleanliness(_coverage_frame("PARTIAL", stream, back)).row(0, named=True)
    assert row["is_clean"] is True
    assert row["reason"] == "clean"


def test_extended_hours_sparsity_is_not_contamination() -> None:
    """Extended-hours minutes (outside 09:30–16:00 ET) are excluded entirely: a name with sparse/zero
    pre/post-market bars but a complete REGULAR session is CLEAN, never flagged for the missing EH minutes.
    Here both sides have a dense regular session (offsets 0..389) and only the STREAM has scattered
    pre-market prints — the EH difference must not affect the verdict."""
    regular = list(range(390))
    # pre-market offsets are negative (before the 09:30 open); only the stream captured a few of them.
    pre_market_stream = [-120, -90, -30]
    frame = _coverage_frame("EH", regular + pre_market_stream, regular)
    cleanliness = symbol_day_cleanliness(frame)
    row = cleanliness.row(0, named=True)
    assert row["is_clean"] is True
    assert row["n_backfill_minutes"] == 390  # EH minutes excluded from the count on both sides
    assert row["max_gap_minutes"] == 0


def test_single_missed_print_within_session_is_clean() -> None:
    """A lone missed minute (<= MAX_GAP_MINUTES) is NOT a restart — the symbol-day stays clean."""
    back = list(range(390))
    stream = [off for off in back if off != 200]  # one isolated missing minute
    cleanliness = symbol_day_cleanliness(_coverage_frame("BLIP", stream, back))
    row = cleanliness.row(0, named=True)
    assert row["max_gap_minutes"] == 1
    assert row["is_clean"] is True


def test_clean_symbols_filters() -> None:
    full = list(range(390))
    half = list(range(150))
    frame = pl.concat([_coverage_frame("FULL", full, full), _coverage_frame("HALF", half, full)])
    cleanliness = symbol_day_cleanliness(frame)
    assert clean_symbols(cleanliness) == ["FULL"]


def test_clean_breadth_floor_suppresses_grading_on_contaminated_day() -> None:
    """A day with fewer clean symbols than MIN_CLEAN_SYMBOLS is too contaminated to grade — the sweep
    contributes no clean-day comparison, so no feature is condemned off a handful of marginal survivors.
    We assert the gate ARITHMETIC (the sweep's I/O is exercised by the live run)."""
    full = list(range(390))
    # one clean name among contaminated ones -> below the breadth floor
    frame = pl.concat(
        [_coverage_frame("ONLY_CLEAN", full, full)]
        + [_coverage_frame(f"DIRTY{i}", list(range(100)), full) for i in range(5)]
    )
    cleanliness = symbol_day_cleanliness(frame)
    clean_count = int(cleanliness["is_clean"].sum())
    assert clean_count == 1
    assert clean_count < MIN_CLEAN_SYMBOLS  # the sweep would skip grading and leave features PENDING


def test_sweep_pins_market_tickers_into_every_chunk(monkeypatch) -> None:
    """The cross-sectional features (market_beta/idio_vol/market_return/...) regress against SPY/QQQ, which
    are ETF-screened out of the raw universe. The sweep MUST pin the market tickers into every materialize +
    validate chunk so the backfill side resolves its market reference — otherwise those features are all
    extra_live (backfill produced nothing) and can never validate. We capture the scope each stage receives
    and assert the market tickers are always present, even though they were never 'discovered' as stream
    symbols."""
    assert set(MARKET_TICKERS) == {"QQQ", "SPY"}
    discovered = ["AAPL", "MSFT", "NVDA"]  # none of these is a market ticker
    materialize_scopes: list[list[str]] = []
    materialize_shards: list[int | None] = []
    validate_scopes: list[list[str]] = []

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])
    # The sweep defaults to the tick-aware materialize (materialize_from_raw_full) so the order-flow groups
    # get a backfill side; patch that (and the bar-only variant, for a --no-ticks sweep) to capture scope.
    # Each chunk is written as its OWN shard so disjoint chunks union on read — capture the shard too.
    def _capture(feature_root, raw_root, day, symbols, shard=None):  # noqa: ANN001,ANN202
        materialize_scopes.append(list(symbols))
        materialize_shards.append(shard)

    monkeypatch.setattr(validation_sweep, "materialize_from_raw_full", _capture)
    monkeypatch.setattr(validation_sweep, "materialize_from_raw", _capture)
    monkeypatch.setattr(
        validation_sweep.validate_mod, "validate",
        lambda feature_root, day, val_root, allow_today, symbols: validate_scopes.append(list(symbols)),
    )
    # short-circuit everything after the chunk loop: no cell/cleanliness -> the breadth-floor early return.
    monkeypatch.setattr(validation_sweep.validation_store, "read_cell", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.validation_store, "read_exceptions", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    validation_sweep.sweep_day("/feat", "/val", "2026-06-12", raw_root="/raw", chunk=2, allow_today=True)

    assert materialize_scopes and validate_scopes
    for scope in materialize_scopes + validate_scopes:
        for ticker in MARKET_TICKERS:
            assert ticker in scope, f"{ticker} must be pinned into every chunk scope"
        assert scope.count("SPY") == 1  # deduped, not double-added when already discovered
    # 3 discovered symbols, chunk=2 -> 2 chunks, each written to a DISTINCT shard so they union on disk.
    assert materialize_shards == list(range(len(materialize_scopes)))


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
