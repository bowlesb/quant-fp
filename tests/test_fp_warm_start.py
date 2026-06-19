"""Warm-start the trailing ring on capture startup == a buffer that was never emptied (CRITICAL-2).

A capture restart (deploy, crash, nightly relaunch) creates a fresh ``CaptureState`` whose ring starts
EMPTY, so for the first ``window`` minutes of streaming every long-window feature lacks its lookback and
collapses/emits NaN — and the same wipe re-corrupts the long windows on every redeploy. ``warm_start_ring``
rehydrates the ring from the session's already-settled bars (``backfill_bars`` = Alpaca historical RAW =
the same unadjusted SIP tape the live stream delivers) BEFORE the first live minute.

Parity is sacred (CLAUDE.md): the warmed ring must hold exactly the rows the live path would itself have
accumulated, so the first live minute after a warm start computes features IDENTICAL to a capture that was
never restarted (``test_warm_start_then_live_minute_matches_cold``). Gated behind ``FP_WARM_START`` (default
OFF): with the flag unset the launch path is byte-identical to today's cold start.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import capture
from quantlib.features.capture import (
    CaptureState,
    process_bars,
    warm_start_enabled,
    warm_start_ring,
)
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import (
    IncrementalEngine,
    WarmStartUnderfilled,
    WindowedSumState,
)

BASE = dt.datetime(2026, 6, 16, 14, 0, tzinfo=dt.timezone.utc)
TICK_VALUES = {
    "n_trades": 10.0, "signed_volume": 5.0, "mean_spread_bps": 2.0,
    "quote_imbalance": 0.1, "mean_bid_size": 3.0, "mean_ask_size": 4.0,
}


def _tick_bars(stream: list[list[dict]]) -> list[list[dict]]:
    """Enrich a bar stream with the 6 tick columns — the 13-col TICK-ENRICHED live frame the real capture
    pushes (vs the 7-col bar-only warm-start seed). Exercises the schema the ShapeError lived in."""
    return [[{**bar, **TICK_VALUES} for bar in batch] for batch in stream]


def _stream_minutes(n_sym: int, n_min: int, seed: int, vol: float = 0.02) -> list[list[dict]]:
    """A normalized-bar stream (per-minute bar batches), every symbol present each minute (a dense session
    — the warm-start source is the settled historical session, which is dense)."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * vol)
            c = price[s]
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        out.append(bars)
    return out


def _bars_frame(stream: list[list[dict]]) -> pl.DataFrame:
    """The settled-bars frame (ring schema) the warm-start source returns, built from a stream's batches."""
    rows = [
        {"symbol": b["S"], "minute": dt.datetime.fromisoformat(b["t"]), "open": b["o"],
         "close": b["c"], "high": b["h"], "low": b["l"], "volume": b["v"]}
        for batch in stream for b in batch
    ]
    return pl.DataFrame(rows, schema=capture.BARS_SCHEMA)


def _assert_frames_match(truth: dict[str, pl.DataFrame], got: dict[str, pl.DataFrame]) -> None:
    """Per-group, joined on (symbol, minute): no null/non-null mismatch and no value beyond parity tolerance.
    Both paths run the IDENTICAL batch compute over an IDENTICAL ring, so this is effectively exact."""
    assert set(truth) == set(got), "group set differs"
    for name, tframe in truth.items():
        gframe = got[name]
        keys = ["symbol", "minute"]
        cols = [c for c in tframe.columns if c not in keys]
        j = tframe.sort(keys).join(gframe.sort(keys).select([*keys, *cols]), on=keys, suffix="__g")
        assert j.height == tframe.height, f"{name}: row set differs"
        for col in cols:
            a, b = pl.col(col), pl.col(f"{col}__g")
            assert j.filter(a.is_null() != b.is_null()).height == 0, f"{name}.{col}: null/non-null mismatch"
            bad = j.filter(
                a.is_not_null() & b.is_not_null() & ((a - b).abs() > 1e-9 + 1e-6 * a.abs())
            )
            assert bad.height == 0, f"{name}.{col}: warm-start != cold on {bad.height} rows"


def test_warm_start_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env set -> warm-start is inert (cold start, byte-identical to today's launch)."""
    monkeypatch.delenv("FP_WARM_START", raising=False)
    assert warm_start_enabled() is False
    monkeypatch.setenv("FP_WARM_START", "1")
    assert warm_start_enabled() is True


def test_warm_start_ring_matches_cold_build() -> None:
    """A ring seeded in one shot from the session bars holds the SAME (symbol, minute) row set as a ring
    built minute-by-minute from the live stream — the warm buffer IS the live buffer."""
    stream = _stream_minutes(n_sym=6, n_min=40, seed=1)
    bars = _bars_frame(stream)

    warm = CaptureState()
    seeded = warm_start_ring(warm, bars, depth=120)
    assert seeded == 40, "expected all 40 distinct minutes seeded"

    cold = CaptureState()
    cold.ring = capture.MinuteRing(maxlen=120)
    for batch in stream:
        cold.ring.push(_bars_frame([batch]))

    w = warm.ring.materialize().sort(["symbol", "minute"])
    c = cold.ring.materialize().sort(["symbol", "minute"])
    assert w.equals(c), "warm-started ring differs from the cold-built ring"


def test_warm_start_respects_depth_cap() -> None:
    """Seeding more minutes than ``depth`` keeps only the TRAILING ``depth`` (the ring's eviction), so a
    warm start never over-fills the buffer past its declared window."""
    stream = _stream_minutes(n_sym=4, n_min=50, seed=2)
    bars = _bars_frame(stream)
    state = CaptureState()
    seeded = warm_start_ring(state, bars, depth=30)
    assert seeded == 30
    minutes = state.ring.materialize()["minute"].unique().sort()
    expected_tail = sorted({dt.datetime.fromisoformat(b["t"]) for b in stream[-1]} | set())  # last minute present
    assert minutes[-1] == expected_tail[0]
    assert minutes.len() == 30
    # the OLDEST 20 minutes were evicted
    assert minutes[0] == BASE + dt.timedelta(minutes=20)


def test_warm_start_empty_bars_noop() -> None:
    """A relaunch before any session bar exists (empty source) leaves the ring untouched and seeds 0."""
    state = CaptureState()
    assert warm_start_ring(state, pl.DataFrame(schema=capture.BARS_SCHEMA), depth=120) == 0
    assert state.ring is None


def test_warm_start_projects_columns() -> None:
    """The reduce-path warm start projects to the reduce groups' columns (parity-neutral subset), exactly as
    the live reduce buffer does."""
    stream = _stream_minutes(n_sym=4, n_min=10, seed=4)
    bars = _bars_frame(stream)
    state = CaptureState()
    warm_start_ring(state, bars, depth=120, project_columns=("symbol", "minute", "close", "volume"))
    assert set(state.ring.materialize().columns) == {"symbol", "minute", "close", "volume"}


def test_warm_start_then_live_minute_matches_cold(tmp_path) -> None:
    """THE parity gate: warm-start the ring from minutes 0..T-1, then process the live minute T, and the
    emitted features for minute T must EQUAL a capture that streamed every minute 0..T from cold (the
    deployed truth). i.e. a restart + warm start is indistinguishable from never having restarted."""
    n_min, window = 30, 120
    stream = _stream_minutes(n_sym=6, n_min=n_min, seed=7)

    # COLD: stream all minutes; the truth is the LAST minute's emitted features.
    cold = CaptureState()
    for batch in stream:
        process_bars(cold, batch, str(tmp_path / "cold"), "mock", "2026-06-16", window, accumulate=True, write=False)
    last_minute = BASE + dt.timedelta(minutes=n_min - 1)
    truth = {g: f.filter(pl.col("minute") == last_minute) for g, f in cold.accumulated.items()}

    # WARM: rehydrate from minutes 0..T-1, then process ONLY the live minute T.
    warm = CaptureState()
    warm_start_ring(warm, _bars_frame(stream[:-1]), depth=window)
    process_bars(warm, stream[-1], str(tmp_path / "warm"), "mock", "2026-06-16", window, accumulate=True, write=False)
    got = warm.accumulated

    assert truth, "expected emitted groups"
    _assert_frames_match(truth, got)


# ``populated`` concept + post-seed assert (Ben-designed): a window is ``populated`` when the state has
# absorbed its full required depth; the post-seed assert catches a FAILED warm-start (data present but not
# absorbed) loudly, while LEGITIMATELY-short history (newly-listed ticker / first day / gap) passes. The
# three-way FULL / legit-not-full / FAILED distinction is the crux — these prove the boundary is right.


def _full_state(span_minutes: int, windows: tuple[int, ...]) -> WindowedSumState:
    """A ``WindowedSumState`` fed ``span_minutes + 1`` consecutive minutes (observed span == span_minutes)."""
    state = WindowedSumState(["A"], windows, 2)
    base = int(BASE.timestamp())
    for i in range(span_minutes + 1):
        state.update(base + i * 60, np.ones((1, 2)))
        state.trim()
    return state


def test_populated_tracks_observed_span_across_trim() -> None:
    """``observed_span_minutes`` / ``populated`` reflect the FULL absorbed history even after ``trim`` evicts
    buffered minutes past the longest window — the span is tracked from first/last folded epoch, not the
    (evicted) buffer. A window is populated iff the absorbed span reaches its full depth."""
    state = _full_state(span_minutes=40, windows=(5, 10, 30, 60))
    assert state.observed_span_minutes() == 40.0
    assert state.populated(5) and state.populated(10) and state.populated(30)
    assert not state.populated(60), "60m window is not yet full on only 40m of history"


def test_assert_populated_full_passes() -> None:
    """FULL arm: every window <= the absorbed span asserts populated; the seed buffer carried that span, so
    the assert passes for all of them."""
    state = _full_state(span_minutes=70, windows=(5, 30, 60))
    state.assert_populated(available_span_minutes=70.0)  # no raise


def test_assert_populated_legit_short_history_no_raise() -> None:
    """LEGITIMATELY not-yet-full arm: the seed buffer ITSELF was shorter than the long window (a newly-listed
    ticker / first day / genuine short history), so the window is correctly not populated — NO raise. This is
    the boundary the assert must NOT cross."""
    state = _full_state(span_minutes=12, windows=(5, 10, 30, 60))
    assert not state.populated(30) and not state.populated(60)
    state.assert_populated(available_span_minutes=12.0)  # buffer only had 12m -> 30/60m legit-short, no raise


def test_assert_populated_failed_warm_start_raises() -> None:
    """warm-start FAILED arm: the seed buffer HAD enough history (available >= window) but the state only
    absorbed a short span (data present, not absorbed — the ShapeError / a dropped slot). The assert RAISES."""
    state = _full_state(span_minutes=12, windows=(5, 10, 30, 60))  # only absorbed 12m
    with pytest.raises(WarmStartUnderfilled, match="30m window"):
        state.assert_populated(available_span_minutes=60.0)  # buffer COULD fill 30/60m, state didn't -> raise


def _engine_over(stream: pl.DataFrame) -> tuple[IncrementalEngine, list[ReductionGroup]]:
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    return IncrementalEngine(groups, warm_start_assert=True), groups


def test_engine_assert_populated_full_history() -> None:
    """Engine-level FULL: after seeding over a buffer DEEPER than every declared window, the assert passes
    and every window reports populated=True."""
    stream = _bars_frame(_stream_minutes(n_sym=6, n_min=10, seed=11))
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    deepest = max(IncrementalEngine(groups).windows)
    n_min = deepest + 30  # deeper than the deepest reduction window (price_levels' 240m)
    stream = _bars_frame(_stream_minutes(n_sym=6, n_min=n_min, seed=11))
    engine, _ = _engine_over(stream)
    engine.seed(stream)  # warm_start_assert=True -> asserts populated; must not raise on a deep buffer
    assert engine.state is not None
    for window in engine.windows:
        assert engine.state.populated(window), f"{window}m not populated after a {n_min}m seed"
    assert engine.warm_start_assert is False, "assert flag should clear after the warm-start seed"


def test_engine_assert_populated_short_history_no_raise() -> None:
    """Engine-level LEGITIMATELY-short: seeding over a SHALLOW buffer (shorter than the long windows, e.g. a
    newly-listed ticker's first few minutes) must NOT raise — the long windows are legitimately not-yet-full.
    This is the unit test the spec requires: a short-history ticker does NOT raise."""
    stream = _bars_frame(_stream_minutes(n_sym=4, n_min=8, seed=12))  # only 8 minutes of history
    engine, _ = _engine_over(stream)
    engine.seed(stream)  # must NOT raise though long windows are unfilled
    assert engine.state is not None
    assert engine.state.observed_span_minutes() == 7.0


def test_engine_assert_populated_broken_seed_raises() -> None:
    """Engine-level FAILED: a seed where the buffer HAS deep history but the absorbed state is short (we
    simulate the failed-absorb by asserting against a larger available span than the state holds) RAISES —
    the ShapeError-class failure surfaced loudly instead of silently under-warming."""
    stream = _bars_frame(_stream_minutes(n_sym=4, n_min=12, seed=13))  # absorbs ~11m
    engine, _ = _engine_over(stream)
    engine.warm_start_assert = False  # seed normally, then assert against a deeper claimed buffer
    engine.seed(stream)
    longest = max(engine.windows)
    with pytest.raises(WarmStartUnderfilled):
        # claim the buffer carried > the longest window of history; the state only absorbed 11m -> failed-absorb
        engine.state.assert_populated(available_span_minutes=float(longest) + 50.0)


def test_warm_start_tick_enriched_no_shape_error(tmp_path) -> None:
    """THE ShapeError fix: warm-start seeds a 7-col BAR-ONLY ring, then the live stream pushes 13-col
    TICK-ENRICHED minutes. ``materialize`` must concat the heterogeneous slots WITHOUT a ShapeError (the
    crash that forced FP_WARM_START off), null-filling the seed minutes' tick columns — honest 'not
    collected', exactly the null a settled premarket bar carries in backfill (parity-correct)."""
    n_min, window = 25, 120
    stream = _stream_minutes(n_sym=5, n_min=n_min, seed=21)

    warm = CaptureState()
    warm_start_ring(warm, _bars_frame(stream[:-1]), depth=window)  # 7-col seed
    assert set(warm.ring.materialize().columns) == set(capture.BARS_SCHEMA), "seed ring is bar-only (7 col)"

    # The live minute arrives tick-enriched (13 col) — the exact path that raised ShapeError before the fix.
    process_bars(warm, _tick_bars([stream[-1]])[0], str(tmp_path), "mock", "2026-06-16", window,
                 accumulate=True, write=False)
    mat = warm.ring.materialize()
    assert set(capture.TICK_COLUMNS) <= set(mat.columns), "tick columns present after the enriched minute"
    last = BASE + dt.timedelta(minutes=n_min - 1)
    seed_minute = mat.filter(pl.col("minute") == BASE)
    live_minute = mat.filter(pl.col("minute") == last)
    assert seed_minute["n_trades"].is_null().all(), "seed minutes carry NULL tick cols (parity-correct)"
    assert (live_minute["n_trades"] == TICK_VALUES["n_trades"]).all(), "live minute carries the tick values"
    assert warm.accumulated, "the enriched live minute emitted features without crashing"


def test_populated_assert_is_value_neutral() -> None:
    """NOT a fingerprint change: the ``populated`` tracking + ``assert_populated`` add state metadata and a
    post-seed check ONLY — they touch no running sum. An engine seeded with ``warm_start_assert=True`` (the
    assert runs) emits features BYTE-IDENTICAL to one seeded with it OFF, over the same buffer. This isolates
    THIS change's contribution: it moves no value.

    (Engine whole-buffer-seed vs minute-by-minute / vs batch for the OLS-conditioning family is a separate
    documented sensitivity — test_fp_incremental_features; the DEPLOYED warm-start parity vs backfill is the
    batch-path gate ``test_warm_start_then_live_minute_matches_cold``.)"""
    stream = _bars_frame(_stream_minutes(n_sym=6, n_min=90, seed=31))
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]

    asserted = IncrementalEngine(groups, warm_start_assert=True)
    asserted_out = asserted.step(stream)  # warm-start seed runs the populated assert, then assembles
    assert asserted.warm_start_assert is False, "warm-start seed ran and cleared the assert flag"

    plain = IncrementalEngine(groups, warm_start_assert=False)
    plain_out = plain.step(stream)

    for group in groups:
        a = asserted_out[group.name].sort("symbol")
        b = plain_out[group.name].sort("symbol").select(a.columns)
        assert a.equals(b), f"{group.name}: populated-assert changed a value (must be byte-identical)"
