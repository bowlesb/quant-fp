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

BASE = dt.datetime(2026, 6, 16, 14, 0, tzinfo=dt.timezone.utc)


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
