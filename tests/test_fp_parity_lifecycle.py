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
import json
import math
import os
import sys

import polars as pl
import pytest

from quantlib.data.raw_backfill import partition_dir

from quantlib.features.cleanliness import (
    MAX_INCOHERENT_FRAC,
    MIN_COVERAGE_FRAC,
    clean_symbols,
    gather_coherence,
    symbol_day_cleanliness,
)
from quantlib.features.trust_lifecycle import (
    AUTO_CLOSE_STREAK,
    DEFECT_STATUS_AUTO_CLOSED,
    DEFECT_STATUS_OPEN,
    MIN_CLEAN_DAYS,
    STATE_DIVERGENT,
    STATE_PENDING,
    STATE_RETIRED,
    STATE_VALIDATED,
    auto_close_updates,
    clean_feature_day,
    defect_rows,
    lifecycle_state,
)
from quantlib.features import validation_sweep
from quantlib.features.base import FeatureType
from quantlib.features.registry import REGISTRY
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


def _clean_cleanliness(symbols: list[str]) -> pl.DataFrame:
    """A cleanliness frame marking exactly ``symbols`` clean (the gradable set the grading pass uses)."""
    return pl.DataFrame({"symbol": symbols, "is_clean": [True] * len(symbols)})


def _coherent() -> dict[str, float | int | bool]:
    """A coherent gather verdict (single-gather day) — the sweep then runs the cross-sectional grade."""
    return {"rth_minutes": 390, "incoherent_minutes": 0, "incoherent_frac": 0.0, "is_coherent": True}


def test_cross_sectional_groups_are_universe_reduce_only() -> None:
    """The discriminator selects the universe-REDUCE groups (a symbol's value depends on the whole present
    universe) and EXCLUDES the reference-relative ones (regress each symbol against a fixed SPY/QQQ, invariant
    to which other symbols are present). The universe-reduce groups are the ones that mis-grade ~0.000 on a
    gradable-subset backfill — the bug this split fixes. Reference-relative groups validate fine on the
    gradable set (the MARKET_TICKERS pin supplies their reference), so they must NOT be diverted."""
    selected = set(validation_sweep.cross_sectional_groups())
    cross_sectional_typed = {g.name for g in REGISTRY.groups() if g.type == FeatureType.CROSS_SECTIONAL}
    # Every selected group is structurally CROSS_SECTIONAL ...
    assert selected.issubset(cross_sectional_typed)
    # ... and the ONLY exclusions from that family are the documented reference-relative groups.
    assert cross_sectional_typed - selected == validation_sweep.REFERENCE_RELATIVE_GROUPS
    # The known universe-reduce groups are all present; the reference-relative ones are all absent.
    assert {"breadth", "cross_sectional_rank", "return_dispersion", "peer_relative"}.issubset(selected)
    assert not ({"market_context", "market_beta"} & selected)


def _stub_split_validation(monkeypatch, compare_calls: list[dict]) -> None:
    """Stub the split-validation surface (``scoped_tiers``/``compare_groups``/``persist_validation``) so a
    sweep test can run without a store, capturing the groups+scope each ``compare_groups`` call received."""

    def _scoped_tiers(day, symbols=None):  # noqa: ANN001,ANN202
        scope = list(symbols) if symbols is not None else []
        return scope, pl.DataFrame({"symbol": scope})

    def _compare(
        feature_root, day, scope_symbols, tiers, groups=None, tolerance_of=None
    ):  # noqa: ANN001,ANN202
        compare_calls.append({"scope": list(scope_symbols), "groups": groups})
        return validation_sweep.validate_mod.CompareResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame())

    monkeypatch.setattr(validation_sweep.validate_mod, "scoped_tiers", _scoped_tiers)
    monkeypatch.setattr(validation_sweep.validate_mod, "compare_groups", _compare)
    monkeypatch.setattr(validation_sweep.validate_mod, "persist_validation", lambda *a, **k: pl.DataFrame())
    # Binary trust grading (docs/TRUST_REDESIGN.md) writes to the DB — stub it so the sweep tests stay
    # network-free; the grant logic itself is covered by tests/test_trust_binary.py.
    monkeypatch.setattr(validation_sweep.trust_binary, "deterministic_features", lambda: [])
    monkeypatch.setattr(validation_sweep.trust_binary, "cell_tolerance_map", lambda: {})
    monkeypatch.setattr(validation_sweep.trust_binary, "feature_policy_map", lambda: {})
    monkeypatch.setattr(validation_sweep.trust_binary, "earned_features", lambda *a, **k: [])
    monkeypatch.setattr(
        validation_sweep.trust_binary,
        "write_trust_grants",
        lambda *a, **k: {"deterministic_trusted": 0, "earned_trusted": 0},
    )


def test_sweep_pins_market_tickers_into_every_chunk(monkeypatch) -> None:
    """The reference-relative features (market_beta/idio_vol/market_return/...) regress against SPY/QQQ,
    which are ETF-screened out of the raw universe. The sweep MUST pin the market tickers into every
    materialize scope and the per-symbol validate scope so the backfill side resolves its market reference —
    otherwise those features are all extra_live (backfill produced nothing) and can never validate. We
    capture the scope each stage receives and assert the market tickers are always present, even though they
    were never 'discovered' as stream symbols."""
    assert set(MARKET_TICKERS) == {"QQQ", "SPY"}
    discovered = ["AAPL", "MSFT", "NVDA"]  # none of these is a market ticker
    bar_scopes: list[list[str]] = []
    full_scopes: list[list[str]] = []
    bar_shards: list[int | None] = []
    full_shards: list[int | None] = []
    compare_calls: list[dict] = []

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep, "assert_raw_present", lambda *a, **k: None)
    monkeypatch.setattr(validation_sweep, "assert_tail_settled", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "tail_settle_status",
        lambda *a, **k: validation_sweep.TailSettleStatus(True, True, 200, 200, 1.0, []),
    )
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])

    def _capture_bar(feature_root, raw_root, day, symbols, shard=None):  # noqa: ANN001,ANN202
        bar_scopes.append(list(symbols))
        bar_shards.append(shard)

    def _capture_full(feature_root, raw_root, day, symbols, shard=None):  # noqa: ANN001,ANN202
        full_scopes.append(list(symbols))
        full_shards.append(shard)

    xsec_bar_scopes: list[list[str]] = []

    def _capture_xsec_bar(feature_root, raw_root, day, symbols, only_groups):  # noqa: ANN001,ANN202
        xsec_bar_scopes.append(list(symbols))

    # PASS 1 is bar-only (materialize_from_raw); PASS 2 is the tick-aware materialize (materialize_from_raw_full).
    # The cross-sectional groups are re-materialized full-universe un-chunked (materialize_from_raw_bar_groups).
    monkeypatch.setattr(validation_sweep, "materialize_from_raw", _capture_bar)
    monkeypatch.setattr(validation_sweep, "materialize_from_raw_full", _capture_full)
    monkeypatch.setattr(validation_sweep, "materialize_from_raw_bar_groups", _capture_xsec_bar)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_groups_day", lambda *a, **k: [])
    _stub_split_validation(monkeypatch, compare_calls)
    monkeypatch.setattr(validation_sweep.validation_store, "read_cell", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.validation_store, "read_exceptions", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        validation_sweep.validation_store, "read_feature_day", lambda *a, **k: pl.DataFrame()
    )
    # All three discovered names are clean -> a gradable day that runs PASS 2 + the split validate.
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: _clean_cleanliness(discovered))
    monkeypatch.setattr(validation_sweep, "day_gather_coherence", lambda *a, **k: _coherent())
    monkeypatch.setattr(validation_sweep, "MIN_CLEAN_SYMBOLS", 1)
    monkeypatch.setattr(validation_sweep, "retired_features", lambda *a, **k: set())
    monkeypatch.setattr(validation_sweep, "lifecycle_state", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep, "defect_rows", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "_build_clean_history", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    validation_sweep.sweep_day("/feat", "/val", "2026-06-12", raw_root="/raw", chunk=2, allow_today=True)

    # The market tickers are pinned into every materialize scope (both passes).
    assert bar_scopes and full_scopes
    for scope in bar_scopes + full_scopes:
        for ticker in MARKET_TICKERS:
            assert ticker in scope, f"{ticker} must be pinned into every materialize scope"
        assert scope.count("SPY") == 1  # deduped, not double-added when already discovered
    # 3 symbols, chunk=2 -> 2 chunks per pass, each written to a DISTINCT shard so they union on disk.
    assert bar_shards == [0, 1]
    assert full_shards == [0, 1]
    # TWO compares: cross-sectional (full universe) + per-symbol (gradable scope, market tickers pinned).
    xsec_groups = validation_sweep.cross_sectional_groups()
    assert len(compare_calls) == 2
    xsec_call = next(call for call in compare_calls if call["groups"] == xsec_groups)
    per_symbol_call = next(call for call in compare_calls if call is not xsec_call)
    assert set(discovered).issubset(xsec_call["scope"])  # cross-sectional graded over the full universe
    for ticker in MARKET_TICKERS:  # per-symbol scope pins the market reference
        assert ticker in per_symbol_call["scope"]
    # The cross-sectional backfill is re-materialized ONCE (un-chunked) over the full universe, so the
    # universe-reduce is a single full-universe compute (not a per-chunk partial-universe one).
    assert len(xsec_bar_scopes) == 1
    assert set(discovered).issubset(set(xsec_bar_scopes[0]))
    # The discriminator: a universe-reduce group is in, the reference-relative groups are out.
    assert "breadth" in xsec_groups
    assert "market_context" not in xsec_groups and "market_beta" not in xsec_groups


def test_sweep_grades_only_the_clean_gradable_set(monkeypatch) -> None:
    """The speedup: PASS 1 materializes the BAR features for ALL discovered symbols (to decide cleanliness),
    but the expensive full-tick PASS 2 materializes ONLY the clean 'gradable' subset — the contaminated
    symbols never reach the costly tick read. We mark a subset clean and assert the two passes see the
    right scopes."""
    discovered = [f"SYM{i}" for i in range(6)]
    clean = discovered[:3]  # only half the day is clean
    bar_seen: set[str] = set()
    full_seen: set[str] = set()
    compare_calls: list[dict] = []

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep, "assert_raw_present", lambda *a, **k: None)
    monkeypatch.setattr(validation_sweep, "assert_tail_settled", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "tail_settle_status",
        lambda *a, **k: validation_sweep.TailSettleStatus(True, True, 200, 200, 1.0, []),
    )
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])
    monkeypatch.setattr(
        validation_sweep,
        "materialize_from_raw",
        lambda fr, rr, day, symbols, shard=None: bar_seen.update(symbols),
    )
    monkeypatch.setattr(
        validation_sweep,
        "materialize_from_raw_full",
        lambda fr, rr, day, symbols, shard=None: full_seen.update(symbols),
    )
    monkeypatch.setattr(
        validation_sweep,
        "materialize_from_raw_bar_groups",
        lambda fr, rr, day, symbols, only_groups: None,
    )
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_groups_day", lambda *a, **k: [])
    _stub_split_validation(monkeypatch, compare_calls)
    monkeypatch.setattr(validation_sweep.validation_store, "read_cell", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.validation_store, "read_exceptions", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        validation_sweep.validation_store, "read_feature_day", lambda *a, **k: pl.DataFrame()
    )
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: _clean_cleanliness(clean))
    monkeypatch.setattr(validation_sweep, "day_gather_coherence", lambda *a, **k: _coherent())
    monkeypatch.setattr(validation_sweep, "MIN_CLEAN_SYMBOLS", 1)
    monkeypatch.setattr(validation_sweep, "retired_features", lambda *a, **k: set())
    monkeypatch.setattr(validation_sweep, "lifecycle_state", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep, "defect_rows", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "_build_clean_history", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    validation_sweep.sweep_day("/feat", "/val", "2026-06-12", raw_root="/raw", chunk=10, allow_today=True)

    # PASS 1 (bar-only) saw every discovered symbol; PASS 2 (full tick) saw ONLY the clean gradable subset.
    assert set(discovered).issubset(bar_seen)
    assert full_seen - set(MARKET_TICKERS) == set(clean)
    assert not (set(discovered) - set(clean)) & full_seen  # no contaminated symbol hit the tick read
    # The cross-sectional compare is graded over the FULL universe (not just the gradable subset) — the fix.
    xsec_groups = validation_sweep.cross_sectional_groups()
    xsec_call = next(call for call in compare_calls if call["groups"] == xsec_groups)
    assert set(discovered).issubset(xsec_call["scope"])
    # The per-symbol compare is scoped to the gradable subset (plus the pinned market reference).
    per_symbol_call = next(call for call in compare_calls if call is not xsec_call)
    assert set(per_symbol_call["scope"]) - set(MARKET_TICKERS) == set(clean)


def test_sweep_skips_full_pass_on_too_contaminated_day(monkeypatch) -> None:
    """When clean breadth is below MIN_CLEAN_SYMBOLS the day cannot grade — the expensive full-tick PASS 2,
    the cross-sectional compare, and the per-symbol compare must NOT run at all (the contaminated-day fast
    exit, BEFORE any compare is attempted)."""
    discovered = [f"SYM{i}" for i in range(6)]
    full_called = {"n": 0}
    compare_calls: list[dict] = []
    persist_called = {"n": 0}

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep, "assert_raw_present", lambda *a, **k: None)
    monkeypatch.setattr(validation_sweep, "assert_tail_settled", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "tail_settle_status",
        lambda *a, **k: validation_sweep.TailSettleStatus(True, True, 200, 200, 1.0, []),
    )
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "materialize_from_raw", lambda *a, **k: None)

    def _full(*a, **k):  # noqa: ANN002,ANN003,ANN202
        full_called["n"] += 1

    monkeypatch.setattr(validation_sweep, "materialize_from_raw_full", _full)
    _stub_split_validation(monkeypatch, compare_calls)
    monkeypatch.setattr(
        validation_sweep.validate_mod,
        "persist_validation",
        lambda *a, **k: persist_called.__setitem__("n", persist_called["n"] + 1) or pl.DataFrame(),
    )
    # only one clean symbol -> below the floor (MIN_CLEAN_SYMBOLS=20)
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: _clean_cleanliness(["SYM0"]))
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    summary = validation_sweep.sweep_day("/feat", "/val", "2026-06-12", raw_root="/raw", allow_today=True)

    assert full_called["n"] == 0  # the costly tick materialize never ran
    assert compare_calls == []  # neither the cross-sectional nor the per-symbol compare ran
    assert persist_called["n"] == 0  # nothing was persisted
    assert summary["features_graded"] == 0
    assert summary["clean_symbols"] == 1


def test_sweep_skips_cross_sectional_grade_on_fragmented_gather(monkeypatch) -> None:
    """A gather-fragmented day (the live universe-wide gather split into concurrent partial-universe
    reductions — a restart / SIP-contention day) must SKIP the cross-sectional grade: those features would
    mis-grade against the single full-universe backfill the contaminated stream can't match. The per-symbol /
    tick PASS 2 still runs (the well-behaved per-symbol features still earn their grade). So exactly ONE
    compare (per-symbol) runs, NOT two, and the cross-sectional groups are NOT re-materialized."""
    discovered = [f"SYM{i}" for i in range(6)]
    clean = discovered[:4]
    full_seen: set[str] = set()
    xsec_materialized = {"n": 0}
    compare_calls: list[dict] = []

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep, "assert_raw_present", lambda *a, **k: None)
    monkeypatch.setattr(validation_sweep, "assert_tail_settled", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "tail_settle_status",
        lambda *a, **k: validation_sweep.TailSettleStatus(True, True, 200, 200, 1.0, []),
    )
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_groups_day", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "materialize_from_raw", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "materialize_from_raw_full",
        lambda fr, rr, day, symbols, shard=None: full_seen.update(symbols),
    )

    def _xsec(*a, **k):  # noqa: ANN002,ANN003,ANN202
        xsec_materialized["n"] += 1

    monkeypatch.setattr(validation_sweep, "materialize_from_raw_bar_groups", _xsec)
    _stub_split_validation(monkeypatch, compare_calls)
    monkeypatch.setattr(validation_sweep.validation_store, "read_cell", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.validation_store, "read_exceptions", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        validation_sweep.validation_store, "read_feature_day", lambda *a, **k: pl.DataFrame()
    )
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: _clean_cleanliness(clean))
    # FRAGMENTED gather: the cross-sectional grade must be skipped.
    monkeypatch.setattr(
        validation_sweep,
        "day_gather_coherence",
        lambda *a, **k: {
            "rth_minutes": 364,
            "incoherent_minutes": 322,
            "incoherent_frac": 0.885,
            "is_coherent": False,
        },
    )
    monkeypatch.setattr(validation_sweep, "MIN_CLEAN_SYMBOLS", 1)
    monkeypatch.setattr(validation_sweep, "retired_features", lambda *a, **k: set())
    monkeypatch.setattr(validation_sweep, "lifecycle_state", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep, "defect_rows", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "_build_clean_history", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    summary = validation_sweep.sweep_day(
        "/feat", "/val", "2026-06-12", raw_root="/raw", chunk=10, allow_today=True
    )

    xsec_groups = set(validation_sweep.cross_sectional_groups())
    assert xsec_materialized["n"] == 0  # the cross-sectional backfill was NOT re-materialized
    # Exactly one compare ran, and it is the PER-SYMBOL one (its groups exclude the cross-sectional set).
    assert len(compare_calls) == 1
    assert not (set(compare_calls[0]["groups"]) & xsec_groups)
    assert summary["gather_coherent"] is False
    assert summary["cross_sectional_graded"] is False
    # PASS 2 still graded the per-symbol features over the clean gradable set.
    assert full_seen - set(MARKET_TICKERS) == set(clean)


def test_sweep_grades_settled_subset_and_skips_xsec_on_unsettled_tail(monkeypatch) -> None:
    """The settled-subset UNBLOCK: a coherent-gather day whose illiquid TAIL has not fully landed must NOT
    abort the whole sweep (the old all-or-nothing RawNotSettledError). Instead it grades the per-symbol PASS 2
    over the settled clean subset (per-symbol features advance trust) and SKIPs ONLY the full-universe
    cross-sectional grade (a partial backfill universe would mis-match its reduction). So exactly ONE compare
    (per-symbol) runs, the xsec backfill is NOT re-materialized, and the summary flags tail_settled False."""
    discovered = [f"SYM{i}" for i in range(6)]
    clean = discovered[:4]
    full_seen: set[str] = set()
    xsec_materialized = {"n": 0}
    compare_calls: list[dict] = []

    monkeypatch.setattr(validation_sweep.validate_mod, "assert_settled", lambda day, allow_today: None)
    monkeypatch.setattr(validation_sweep, "assert_raw_present", lambda *a, **k: None)
    # TAIL only partially settled (coherent gather, but the universe backfill is incomplete).
    monkeypatch.setattr(
        validation_sweep,
        "tail_settle_status",
        lambda *a, **k: validation_sweep.TailSettleStatus(False, True, 200, 120, 0.60, ["ABR.PRE", "ACGC"]),
    )
    monkeypatch.setattr(validation_sweep.store, "stream_symbols_on", lambda *a, **k: discovered)
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_day", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep.store, "clear_backfill_groups_day", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "materialize_from_raw", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_sweep,
        "materialize_from_raw_full",
        lambda fr, rr, day, symbols, shard=None: full_seen.update(symbols),
    )

    def _xsec(*a, **k):  # noqa: ANN002,ANN003,ANN202
        xsec_materialized["n"] += 1

    monkeypatch.setattr(validation_sweep, "materialize_from_raw_bar_groups", _xsec)
    _stub_split_validation(monkeypatch, compare_calls)
    monkeypatch.setattr(validation_sweep.validation_store, "read_cell", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.validation_store, "read_exceptions", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        validation_sweep.validation_store, "read_feature_day", lambda *a, **k: pl.DataFrame()
    )
    monkeypatch.setattr(validation_sweep, "day_cleanliness", lambda *a, **k: _clean_cleanliness(clean))
    # Gather IS coherent — only the unsettled tail gates the xsec grade here.
    monkeypatch.setattr(
        validation_sweep,
        "day_gather_coherence",
        lambda *a, **k: {
            "rth_minutes": 364,
            "incoherent_minutes": 0,
            "incoherent_frac": 0.0,
            "is_coherent": True,
        },
    )
    monkeypatch.setattr(validation_sweep, "MIN_CLEAN_SYMBOLS", 1)
    monkeypatch.setattr(validation_sweep, "retired_features", lambda *a, **k: set())
    monkeypatch.setattr(validation_sweep, "lifecycle_state", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep, "defect_rows", lambda *a, **k: [])
    monkeypatch.setattr(validation_sweep, "_build_clean_history", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(validation_sweep.trust_lifecycle, "write_lifecycle", lambda *a, **k: None)

    summary = validation_sweep.sweep_day(
        "/feat", "/val", "2026-06-12", raw_root="/raw", chunk=10, allow_today=True
    )

    xsec_groups = set(validation_sweep.cross_sectional_groups())
    assert xsec_materialized["n"] == 0  # xsec backfill NOT re-materialized (skipped on unsettled tail)
    assert len(compare_calls) == 1  # only the per-symbol compare ran
    assert not (set(compare_calls[0]["groups"]) & xsec_groups)
    assert summary["gather_coherent"] is True  # the gate that fired was the tail, not the gather
    assert summary["tail_settled"] is False
    assert summary["cross_sectional_graded"] is False
    # the UNBLOCK: PASS 2 still graded the per-symbol features over the settled clean subset.
    assert full_seen - set(MARKET_TICKERS) == set(clean)


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
            _cell(
                "feat", "DIRTY", n_match=100, n_mismatch=290
            ),  # contaminated symbol: would fail if counted
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
    history = pl.DataFrame(
        [_clean_day_row("feat", "2026-06-10", passed=True)]
    )  # 1 clean day < MIN_CLEAN_DAYS
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


def test_defect_rows_sanitize_non_finite_exemplar_values() -> None:
    """A stream feature can emit a non-finite value (Infinity / -Infinity / NaN) — a real mismatch to
    record, not a crash. ``json.dumps(inf)`` emits the bare token ``Infinity`` which the exemplars jsonb
    column rejects, so the defect-row builder must NULL non-finite values before serialize. The resulting
    JSON must be STRICTLY valid (no Infinity/NaN tokens) and the non-finite values become null."""
    history = pl.DataFrame([_clean_day_row("feat", "2026-06-15", passed=False)])
    states = lifecycle_state(history, retired=set())
    exceptions = pl.DataFrame(
        {
            "feature": ["feat", "feat", "feat"],
            "symbol": ["INF", "NEGINF", "NAN"],
            "minute": [_minute(0), _minute(1), _minute(2)],
            "stream_value": [math.inf, -math.inf, math.nan],
            "backfill_value": [1.0, 1.0, 1.0],
            "rel_err": [math.inf, math.inf, math.nan],
        }
    )
    rows = defect_rows(states, history, exceptions, group_of={"feat": "grp"}, version_of={"feat": "1.0.0"})
    assert len(rows) == 1
    worst_rel, exemplars_json = rows[0][6], rows[0][7]
    assert worst_rel is None  # max rel_err was Infinity -> NULLed

    # STRICT JSON parse (Python's json ACCEPTS Infinity/NaN by default; force the standard so a leaked
    # non-finite token raises — exactly what Postgres jsonb would reject).
    parsed = json.loads(exemplars_json, parse_constant=_reject_non_finite)
    assert len(parsed) == 3
    assert [cell["stream_value"] for cell in parsed] == [None, None, None]  # inf/-inf/nan -> null
    assert all(cell["backfill_value"] == 1.0 for cell in parsed)  # finite values preserved
    assert [cell["rel_err"] for cell in parsed] == [None, None, None]


def _reject_non_finite(token: str) -> float:
    raise ValueError(f"non-finite JSON token leaked into exemplars: {token}")


def test_no_defect_when_no_divergent() -> None:
    history = pl.DataFrame(
        [_clean_day_row("feat", f"2026-06-{10 + i:02d}", passed=True) for i in range(MIN_CLEAN_DAYS)]
    )
    states = lifecycle_state(history, retired=set())
    assert (
        defect_rows(states, history, pl.DataFrame(), group_of={"feat": "g"}, version_of={"feat": "1"}) == []
    )


def _broadcast_breadth(n_symbols: int, per_minute_values: list[list[float]]) -> pl.DataFrame:
    """A stream frame for one broadcast cross-sectional scalar. ``per_minute_values[m]`` is the list of
    distinct scalar values present at RTH minute ``m`` — a clean minute lists ONE value (shared by every
    symbol), a fragmented minute lists several (the partial-universe gathers). The symbols are partitioned
    across the listed values so each value appears on some symbols' rows."""
    symbols, minutes, values = [], [], []
    for minute_index, distinct_values in enumerate(per_minute_values):
        for symbol_index in range(n_symbols):
            symbols.append(f"S{symbol_index:04d}")
            minutes.append(_minute(minute_index))
            values.append(distinct_values[symbol_index % len(distinct_values)])
    return pl.DataFrame({"symbol": symbols, "minute": minutes, "breadth_up_5m": values})


def test_gather_coherence_clean_single_gather() -> None:
    """A clean single-gather day: every RTH minute has ONE distinct broadcast value -> coherent."""
    frame = _broadcast_breadth(50, [[0.5 + 0.001 * m] for m in range(120)])  # one value per minute
    verdict = gather_coherence(frame, "breadth_up_5m")
    assert verdict["rth_minutes"] == 120
    assert verdict["incoherent_minutes"] == 0
    assert verdict["incoherent_frac"] == 0.0
    assert verdict["is_coherent"] is True


def test_gather_coherence_fragmented_day_flagged() -> None:
    """A capture-restart / contention day: most RTH minutes carry SEVERAL distinct breadth values (the
    concurrent partial-universe gathers) -> incoherent_frac high -> flagged NOT coherent. This is the
    2026-06-15 signature (322/364 minutes multi-valued) the per-symbol coverage check cannot see."""
    per_minute = [[0.4, 0.5, 0.6, 0.7] for _ in range(110)] + [
        [0.5] for _ in range(10)
    ]  # 110/120 fragmented
    frame = _broadcast_breadth(50, per_minute)
    verdict = gather_coherence(frame, "breadth_up_5m")
    assert verdict["incoherent_minutes"] == 110
    assert verdict["incoherent_frac"] > MAX_INCOHERENT_FRAC
    assert verdict["is_coherent"] is False


def test_gather_coherence_tolerates_rare_blips() -> None:
    """A handful of incoherent minutes (below MAX_INCOHERENT_FRAC) does NOT condemn an otherwise clean day —
    the gate flags systematic fragmentation, not isolated single-minute noise."""
    blips = max(1, int(120 * MAX_INCOHERENT_FRAC))  # within tolerance
    per_minute = [[0.4, 0.6] for _ in range(blips)] + [[0.5] for _ in range(120 - blips)]
    frame = _broadcast_breadth(50, per_minute)
    verdict = gather_coherence(frame, "breadth_up_5m")
    assert verdict["incoherent_minutes"] == blips
    assert verdict["is_coherent"] is True


def test_gather_coherence_excludes_extended_hours() -> None:
    """Extended-hours minutes are never counted — only the regular session is checked. A pre-market minute
    (before 09:30 ET) with multiple values must not register as incoherent."""
    pre = pl.DataFrame(
        {
            "symbol": ["A", "B", "A", "B"],
            "minute": [_minute(-30), _minute(-30), _minute(0), _minute(0)],
            "breadth_up_5m": [0.1, 0.9, 0.5, 0.5],  # pre-market disagrees; RTH minute 0 agrees
        }
    )
    verdict = gather_coherence(pre, "breadth_up_5m")
    assert verdict["rth_minutes"] == 1  # only minute 0 (RTH); the -30 pre-market minute excluded
    assert verdict["incoherent_minutes"] == 0
    assert verdict["is_coherent"] is True


def test_gather_coherence_empty_is_vacuously_coherent() -> None:
    """No breadth captured -> nothing to certify here (the per-symbol checks still gate grading)."""
    verdict = gather_coherence(
        pl.DataFrame({"symbol": [], "minute": [], "breadth_up_5m": []}), "breadth_up_5m"
    )
    assert verdict["rth_minutes"] == 0
    assert verdict["is_coherent"] is True


def _write_raw_bar(raw_root: str, symbol: str, day: str, rows: int = 200) -> None:
    """A settled raw BARS partition (the loader-relevant columns) for the presence probe. ``rows`` defaults
    to a real-session count (above MIN_MARKET_TICKER_BARS); pass a small value to emulate a pre-session stub.
    """
    target = dt.date.fromisoformat(day)
    out = partition_dir(raw_root, "bars", symbol, target)
    os.makedirs(out, exist_ok=True)
    minutes = [OPEN_ET + dt.timedelta(minutes=i) for i in range(rows)]
    pl.DataFrame(
        {
            "symbol": [symbol] * rows,
            "ts": minutes,
            "open": [10.0] * rows,
            "close": [10.1] * rows,
            "high": [10.2] * rows,
            "low": [9.9] * rows,
            "volume": [1000] * rows,
        }
    ).write_parquet(os.path.join(out, "data.parquet"))


def _write_raw_trade(raw_root: str, symbol: str, day: str, rows: int = 200) -> None:
    """A settled raw TRADES partition for the with-ticks presence probe. ``rows`` defaults to a real-session
    count (above MIN_MARKET_TICKER_TRADES); pass a small value to emulate a partially-settled stub tape."""
    target = dt.date.fromisoformat(day)
    out = partition_dir(raw_root, "trades", symbol, target)
    os.makedirs(out, exist_ok=True)
    timestamps = [OPEN_ET + dt.timedelta(seconds=i) for i in range(rows)]
    pl.DataFrame(
        {
            "symbol": [symbol] * rows,
            "ts": timestamps,
            "price": [10.05] * rows,
            "size": [100] * rows,
        }
    ).write_parquet(os.path.join(out, "data.parquet"))


def test_assert_raw_present_refuses_unsettled_day(tmp_path) -> None:
    """A closed-but-unsettled day has EMPTY raw partitions (Alpaca lands ~T+1) — the sweep must refuse it
    with an actionable error instead of silently mis-grading every symbol as no_raw."""
    with pytest.raises(ValueError, match="raw BARS are empty"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=True)


def test_assert_raw_present_refuses_when_only_bars_settled_with_ticks(tmp_path) -> None:
    """Bars can settle before trades. A WITH-TICKS sweep needs the order-flow backfill side, so empty
    trades must still refuse (the bar/cross-sectional groups can be swept bar-only instead)."""
    for ticker in MARKET_TICKERS:
        _write_raw_bar(str(tmp_path), ticker, "2026-06-18")
    with pytest.raises(ValueError, match="raw TRADES are empty"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=True)


def test_assert_raw_present_bar_only_sweep_ignores_missing_trades(tmp_path) -> None:
    """A bar-only sweep (--no-ticks) grades only bar/cross-sectional groups, so it needs bars but NOT
    trades — present bars alone must pass."""
    for ticker in MARKET_TICKERS:
        _write_raw_bar(str(tmp_path), ticker, "2026-06-18")
    validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=False)


def test_assert_raw_present_passes_when_settled(tmp_path) -> None:
    """A fully settled day (bars AND trades present for the pinned market tickers) passes the probe."""
    for ticker in MARKET_TICKERS:
        _write_raw_bar(str(tmp_path), ticker, "2026-06-18")
        _write_raw_trade(str(tmp_path), ticker, "2026-06-18")
    validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=True)


def test_assert_raw_present_refuses_when_one_ticker_bars_empty(tmp_path) -> None:
    """On a half-acquired day one pinned ticker can land a full bar tape while the OTHER is missing. The
    union has rows>0, but a market reference is absent — checking each ticker individually must refuse."""
    settled = MARKET_TICKERS[0]
    _write_raw_bar(str(tmp_path), settled, "2026-06-18")
    with pytest.raises(ValueError, match="raw BARS are empty/stub"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=False)


def test_assert_raw_present_refuses_stub_bars_below_floor(tmp_path) -> None:
    """A pre-session STUB bar partition (a handful of rows the idempotent resume never re-fetched) is not a
    settled tape — a per-ticker ROW FLOOR must reject it even though the partition exists with rows>0."""
    for ticker in MARKET_TICKERS:
        _write_raw_bar(str(tmp_path), ticker, "2026-06-18", rows=3)
    with pytest.raises(ValueError, match="raw BARS are empty/stub"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=False)


def test_assert_raw_present_refuses_stub_trades_below_floor(tmp_path) -> None:
    """The exact 06-18 footgun: one pinned ticker lands a full trade tape while the other lands a 2-row STUB
    (the manifest recorded rows=2 'done', so the resume never re-fetched). A union height>0 hid it; the
    per-ticker trade floor must refuse the with-ticks sweep."""
    for ticker in MARKET_TICKERS:
        _write_raw_bar(str(tmp_path), ticker, "2026-06-18")
    _write_raw_trade(str(tmp_path), MARKET_TICKERS[0], "2026-06-18")  # full tape
    _write_raw_trade(str(tmp_path), MARKET_TICKERS[1], "2026-06-18", rows=2)  # stub
    with pytest.raises(ValueError, match="raw TRADES are empty/stub"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=True)


def _settled_tail(raw_root: str, symbols: list[str], day: str, rate: float) -> None:
    """Land settled raw BARS for the first ``rate`` fraction of ``symbols`` (the rest have NO backfill yet) —
    emulates Alpaca's symbol-by-symbol historical fetch having reached only part of the illiquid tail."""
    n_settled = int(round(len(symbols) * rate))
    for symbol in symbols[:n_settled]:
        _write_raw_bar(raw_root, symbol, day)


def test_assert_tail_settled_refuses_partially_settled_universe(tmp_path) -> None:
    """The 2026-06-18 mis-grade: SPY/QQQ settled (assert_raw_present passed) but the illiquid TAIL had not —
    streamed thin names had NO backfill bars, so the sweep filed ~450 false stream>0/backfill=0 defects. The
    tail probe samples the discovered universe and refuses when the backfill present-rate is below the floor.
    """
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.50)  # half the tail not landed yet
    with pytest.raises(ValueError, match="ILLIQUID TAIL has not settled"):
        validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), discovered)


def test_assert_tail_settled_passes_when_tail_landed(tmp_path) -> None:
    """A fully settled day has landed raw bars for essentially every streamed symbol — the sample present-rate
    clears the floor and the probe passes (tolerating the few percent of delisted/halted names with no raw).
    """
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.97)  # 97% landed (a couple no-raw names)
    validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), discovered)


def test_assert_tail_settled_rejects_stub_bars_in_tail(tmp_path) -> None:
    """A few pre-session STUB bar rows are not a landed tape — a tail symbol below the bar floor counts as
    unsettled, so a universe of stubs is refused just like an empty one."""
    discovered = [f"T{i:04d}" for i in range(400)]
    for symbol in discovered:
        _write_raw_bar(str(tmp_path), symbol, "2026-06-18", rows=3)  # all stubs
    with pytest.raises(ValueError, match="ILLIQUID TAIL has not settled"):
        validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), discovered)


def test_assert_tail_settled_noops_on_tiny_universe(tmp_path) -> None:
    """A sandbox universe of just the market tickers has nothing beyond them to probe — the tail check is a
    no-op there (assert_raw_present already certifies the pinned tickers); it must not raise."""
    validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), list(MARKET_TICKERS))


def test_tail_settle_status_reports_partial_split_without_raising(tmp_path) -> None:
    """The settled-subset unblock: a partially-settled tail must NOT abort the day — tail_settle_status returns
    is_settled=False with the rate + a few unsettled examples, so the caller grades the settled subset and skips
    ONLY the full-universe cross-sectional grade (vs the old all-or-nothing RawNotSettledError abort)."""
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.50)  # half the tail not landed yet
    status = validation_sweep.tail_settle_status("2026-06-18", str(tmp_path), discovered)
    assert status.is_settled is False
    assert status.probed is True
    assert status.settle_rate < validation_sweep.MIN_TAIL_SETTLE_RATE
    assert status.settled_count < status.sampled
    assert status.unsettled_examples  # a few names for the operator log


def test_tail_settle_status_is_settled_when_tail_landed(tmp_path) -> None:
    """A fully settled day clears the floor — is_settled True, no unsettled examples — so the cross-sectional
    grade proceeds exactly as before (no behavior change on the happy path)."""
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.97)  # 97% landed
    status = validation_sweep.tail_settle_status("2026-06-18", str(tmp_path), discovered)
    assert status.is_settled is True
    assert status.settle_rate >= validation_sweep.MIN_TAIL_SETTLE_RATE


def test_tail_settle_status_tiny_universe_is_vacuously_settled(tmp_path) -> None:
    """A sandbox universe of just the market tickers has nothing beyond them to probe — probed False, settled
    True (assert_raw_present already certified the pinned tickers); the xsec grade is not gated off here."""
    status = validation_sweep.tail_settle_status("2026-06-18", str(tmp_path), list(MARKET_TICKERS))
    assert status.is_settled is True
    assert status.probed is False


def test_assert_tail_settled_still_raises_on_partial_tail(tmp_path) -> None:
    """The strict assert_ wrapper is retained (back-compat for callers wanting all-or-nothing); it delegates to
    tail_settle_status and raises RawNotSettledError when the tail is not fully settled."""
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.50)
    with pytest.raises(validation_sweep.RawNotSettledError, match="ILLIQUID TAIL has not settled"):
        validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), discovered)


def test_tail_settle_sample_is_deterministic_and_excludes_market_tickers() -> None:
    """The probe sample is day-seeded (idempotent across re-runs of the same day) and never includes the
    pinned market tickers (they settle first and would bias the tail present-rate upward)."""
    discovered = list(MARKET_TICKERS) + [f"T{i:04d}" for i in range(500)]
    sample_a = validation_sweep._sample_universe(
        discovered, "2026-06-18", validation_sweep.TAIL_SETTLE_SAMPLE
    )
    sample_b = validation_sweep._sample_universe(
        discovered, "2026-06-18", validation_sweep.TAIL_SETTLE_SAMPLE
    )
    assert sample_a == sample_b
    assert len(sample_a) == validation_sweep.TAIL_SETTLE_SAMPLE
    assert not (set(sample_a) & set(MARKET_TICKERS))


def test_settle_gates_raise_raw_not_settled_subclass_of_value_error(tmp_path) -> None:
    """The settle gates raise the NARROWER RawNotSettledError so the lifecycle cron can distinguish "skip,
    retry once landed" from a genuine error — while staying a ValueError subclass so every existing
    ``except ValueError`` / ``pytest.raises(ValueError)`` caller is unchanged."""
    assert issubclass(validation_sweep.RawNotSettledError, ValueError)
    with pytest.raises(validation_sweep.RawNotSettledError, match="raw BARS are empty"):
        validation_sweep.assert_raw_present("2026-06-18", str(tmp_path), with_ticks=False)
    discovered = [f"T{i:04d}" for i in range(400)]
    _settled_tail(str(tmp_path), discovered, "2026-06-18", rate=0.50)
    with pytest.raises(validation_sweep.RawNotSettledError, match="ILLIQUID TAIL has not settled"):
        validation_sweep.assert_tail_settled("2026-06-18", str(tmp_path), discovered)


def test_main_skips_cleanly_on_unsettled_day(monkeypatch, capsys) -> None:
    """The exact daily_lifecycle footgun: the post-close nightly sweep targets a day whose raw has not landed
    yet (Alpaca ~T+1), so the settle gate raises RawNotSettledError. main() must SKIP it cleanly (no raise,
    exit 0 → /jobs grades SKIPPED, not FAILED), not propagate the error and fail the whole cron job."""

    def _raise_not_settled(**kwargs: object) -> dict[str, object]:
        raise validation_sweep.RawNotSettledError("refusing to sweep 2026-06-18: raw BARS are empty")

    monkeypatch.setattr(validation_sweep, "sweep_day", _raise_not_settled)
    monkeypatch.setattr(sys, "argv", ["validation_sweep", "2026-06-18", "/feat", "/val", "/raw"])
    validation_sweep.main()  # must NOT raise
    assert "SKIPPED for 2026-06-18 (raw not settled)" in capsys.readouterr().out


def test_main_still_fails_on_a_genuine_error(monkeypatch) -> None:
    """The skip is NARROW: only RawNotSettledError is swallowed. A genuine error (any other exception) still
    propagates and fails the job — we never want a real sweep bug graded as a benign skip."""

    def _raise_real_error(**kwargs: object) -> dict[str, object]:
        raise ValueError("a real sweep bug")

    monkeypatch.setattr(validation_sweep, "sweep_day", _raise_real_error)
    monkeypatch.setattr(sys, "argv", ["validation_sweep", "2026-06-18", "/feat", "/val", "/raw"])
    with pytest.raises(ValueError, match="a real sweep bug"):
        validation_sweep.main()


def test_auto_close_increments_streak_on_a_clean_recurrence_free_day() -> None:
    """An OPEN defect whose feature graded CLEAN (recurrence-free) this sweep advances its streak by one.
    Below the target it stays OPEN — one clean day is not enough to close a real defect."""
    open_defects = [("feat", "1.0.0", 0, None)]
    updates = auto_close_updates(open_defects, {"feat"}, set(), "2026-06-22", streak_target=3)
    assert updates == [("feat", "1.0.0", 1, DEFECT_STATUS_OPEN, "2026-06-22")]


def test_auto_close_resets_streak_on_recurrence() -> None:
    """A genuine recurrence (feature re-failed parity on a clean symbol-day) is excluded from the auto-close
    updates entirely — the defect UPSERT path resets its streak to 0 + re-opens it, so auto_close_updates
    must NOT emit a competing write for it (no double-write, and the streak never advances on a fail)."""
    open_defects = [("feat", "1.0.0", 1, "2026-06-21")]  # had a clean streak going
    updates = auto_close_updates(open_defects, set(), {"feat"}, "2026-06-22", streak_target=3)
    assert updates == []  # left to the recurrence upsert (streak -> 0)


def test_auto_close_flips_to_auto_closed_at_target() -> None:
    """When the streak reaches AUTO_CLOSE_STREAK the defect flips open -> auto_closed (a status DISTINCT from
    the manual 'fixed' so the provenance is auto vs hand-cleared)."""
    open_defects = [("feat", "1.0.0", AUTO_CLOSE_STREAK - 1, "2026-06-21")]
    updates = auto_close_updates(open_defects, {"feat"}, set(), "2026-06-22")
    assert updates == [("feat", "1.0.0", AUTO_CLOSE_STREAK, DEFECT_STATUS_AUTO_CLOSED, "2026-06-22")]


def test_auto_close_ignores_a_feature_not_graded_this_day() -> None:
    """THE contamination guard: a feature that was NOT graded clean this sweep (its day was contaminated /
    skipped / it never appeared) is neither in graded_clean nor recurred — its streak must NOT advance. A
    skipped or contaminated day can never count as a clean recurrence-free day."""
    open_defects = [("feat", "1.0.0", 1, "2026-06-20")]
    updates = auto_close_updates(open_defects, set(), set(), "2026-06-22")
    assert updates == []  # untouched -> streak stays 1


def test_auto_close_is_idempotent_when_the_same_day_re_runs() -> None:
    """Per-DAY idempotency: the streak counts distinct clean DAYS, not sweep invocations. A defect whose
    last_streak_day already equals this day was counted on a prior run of the SAME day -> re-running the
    sweep must NOT advance it again (an operator re-sweep can't double-close a defect)."""
    open_defects = [("feat", "1.0.0", 1, "2026-06-22")]  # already advanced on 06-22
    updates = auto_close_updates(open_defects, {"feat"}, set(), "2026-06-22")
    assert updates == []  # same day -> no-op


def test_auto_close_advances_only_the_clean_subset_across_a_mixed_sweep() -> None:
    """A realistic sweep: one open defect grades clean (advances), one recurs (reset, excluded here), one
    isn't graded at all (untouched). Only the clean recurrence-free one produces an update."""
    open_defects = [
        ("clean", "1.0.0", 0, None),
        ("recur", "1.0.0", 1, "2026-06-21"),
        ("absent", "1.0.0", 1, "2026-06-20"),
    ]
    updates = auto_close_updates(open_defects, {"clean"}, {"recur"}, "2026-06-22", streak_target=3)
    assert updates == [("clean", "1.0.0", 1, DEFECT_STATUS_OPEN, "2026-06-22")]


def test_auto_close_two_consecutive_clean_days_close_at_default_target() -> None:
    """End-to-end streak progression at the DEFAULT AUTO_CLOSE_STREAK (2): day 1 advances 0->1 (still open),
    day 2 advances 1->2 and auto-closes. Proves the chosen N closes exactly on the 2nd distinct clean day."""
    day1 = auto_close_updates([("feat", "1.0.0", 0, None)], {"feat"}, set(), "2026-06-22")
    assert day1 == [("feat", "1.0.0", 1, DEFECT_STATUS_OPEN, "2026-06-22")]
    day2 = auto_close_updates([("feat", "1.0.0", 1, "2026-06-22")], {"feat"}, set(), "2026-06-23")
    assert day2 == [("feat", "1.0.0", 2, DEFECT_STATUS_AUTO_CLOSED, "2026-06-23")]
    assert AUTO_CLOSE_STREAK == 2
