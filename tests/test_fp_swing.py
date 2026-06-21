"""Parity gates for the SWING / ZigZag structure feature (the point-in-time ZigZag fold).

Four invariants, the FIRST two being the reason this feature is interesting:

  1. NO-LOOK-AHEAD — the value emitted at minute T over a buffer ending at T is IDENTICAL whether or not bars
     after T exist. A standard ZigZag repaints (confirms pivots with future bars); this fold must NOT, so a
     pivot at T is confirmed only once the theta-reversal has actually occurred by T. Checked at every T by
     comparing ``compute(buffer<=T).at(T)`` against ``compute(buffer<=T+k).at(T)`` for a growing k.
  2. KIND invariant (fold == reseed) — folding one more minute equals re-seeding the fold with that minute
     appended, cell-for-cell INCLUDING warmup. The kernel folds the whole buffer fresh each minute, so this is
     ``compute(H+m).at(m)`` == the fold replayed over H then m — which here IS the same single ordered pass, so
     it reduces to the no-look-ahead invariant plus a Python-reference pin.
  3. PYTHON REFERENCE PIN — a pure-Python re-implementation of the fold equals the Rust kernel cell-for-cell at
     every (symbol, minute), the same shape ``test_fp_rust.py`` pins ``tick_runlength``.
  4. GROUP parity — ``compute_latest`` == ``compute().filter(last minute)``, the generic live==backfill gate.
"""
from __future__ import annotations

import datetime as dt
from collections import deque

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.swing import (
    DAY_SECS,
    FIB_MAX_ABS,
    RING_K,
    THETA,
    SwingGroup,
    swing_fold_frame,
)

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)
_FEATURES = (
    "swing_dir",
    "swing_steepness",
    "swing_len_pct",
    "minutes_since_pivot",
    "n_pivots_today",
    "n_alternations",
    "swing_persistence",
    "fib_retracement",
    "trend_resolved",
)


def _stream(n_sym: int = 4, n_min: int = 150, seed: int = 11, vol: float = 0.004) -> pl.DataFrame:
    """A noisy close stream that genuinely swings (vol >> theta so pivots actually confirm)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + 5.0 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + rng.standard_normal() * vol
            rows.append({"symbol": f"S{s}", "minute": minute, "close": price[s]})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _python_swing(closes: list[float], minutes: list[int]) -> dict[str, list[float]]:
    """The point-in-time swing/zigzag fold in pure Python — the reference the Rust kernel must equal cell-for-
    cell. One ordered pass over a single symbol's (close, minute) series; emits each bar's POINT-IN-TIME state
    using ONLY bars up to and including that bar (no look-ahead)."""
    out: dict[str, list[float]] = {name: [] for name in _FEATURES}
    direction = 0
    leg_start_price = float("nan")
    leg_start_min = 0
    extreme = float("nan")
    extreme_min = 0
    hi = float("nan")
    hi_min = 0
    lo = float("nan")
    lo_min = 0
    prev_leg_start = float("nan")
    prev_leg_end = float("nan")
    have_prev_leg = False
    n_pivots_today = 0.0
    cur_day = None
    leg_returns: deque[float] = deque()
    leg_steeps: deque[float] = deque()
    n_alternations = 0.0

    def push_pivot(pivot_price: float, start_price: float, span_secs: int) -> None:
        nonlocal prev_leg_start, prev_leg_end, have_prev_leg
        signed_ret = (pivot_price - start_price) / start_price if start_price > 0.0 else 0.0
        mins = span_secs // 60
        steep = signed_ret / mins if mins > 0 else 0.0
        prev_leg_start = start_price
        prev_leg_end = pivot_price
        have_prev_leg = True
        leg_returns.append(signed_ret)
        leg_steeps.append(steep)
        while len(leg_returns) > RING_K:
            leg_returns.popleft()
        while len(leg_steeps) > RING_K:
            leg_steeps.popleft()

    for close, minute in zip(closes, minutes):
        day = minute // DAY_SECS
        if day != cur_day:
            cur_day = day
            n_pivots_today = 0.0
        if leg_start_price != leg_start_price:  # nan -> first bar
            leg_start_price = close
            leg_start_min = minute
            extreme = close
            extreme_min = minute
            hi = lo = close
            hi_min = lo_min = minute
        elif direction == 0:
            if close > hi:
                hi, hi_min = close, minute
            if close < lo:
                lo, lo_min = close, minute
            down_rev = (hi - close) / hi if hi > 0.0 else 0.0
            up_rev = (close - lo) / lo if lo > 0.0 else 0.0
            if down_rev >= THETA and down_rev >= up_rev:
                push_pivot(hi, leg_start_price, hi_min - leg_start_min)
                n_pivots_today += 1.0
                n_alternations += 1.0
                direction = -1
                leg_start_price, leg_start_min = hi, hi_min
                extreme, extreme_min = close, minute
            elif up_rev >= THETA:
                push_pivot(lo, leg_start_price, lo_min - leg_start_min)
                n_pivots_today += 1.0
                n_alternations += 1.0
                direction = 1
                leg_start_price, leg_start_min = lo, lo_min
                extreme, extreme_min = close, minute
        elif direction == 1:
            if close >= extreme:
                extreme, extreme_min = close, minute
            elif extreme > 0.0 and (extreme - close) / extreme >= THETA:
                push_pivot(extreme, leg_start_price, extreme_min - leg_start_min)
                n_pivots_today += 1.0
                n_alternations += 1.0
                direction = -1
                leg_start_price, leg_start_min = extreme, extreme_min
                extreme, extreme_min = close, minute
        else:
            if close <= extreme:
                extreme, extreme_min = close, minute
            elif extreme > 0.0 and (close - extreme) / extreme >= THETA:
                push_pivot(extreme, leg_start_price, extreme_min - leg_start_min)
                n_pivots_today += 1.0
                n_alternations += 1.0
                direction = 1
                leg_start_price, leg_start_min = extreme, extreme_min
                extreme, extreme_min = close, minute

        len_pct = (close - leg_start_price) / leg_start_price if leg_start_price > 0.0 else 0.0
        mins = (minute - leg_start_min) // 60
        steep = len_pct / mins if mins > 0 else 0.0
        out["swing_dir"].append(float(direction))
        out["swing_len_pct"].append(len_pct)
        out["swing_steepness"].append(steep)
        out["minutes_since_pivot"].append(float(mins) if direction != 0 else float("nan"))
        out["n_pivots_today"].append(n_pivots_today)
        out["n_alternations"].append(n_alternations)
        out["swing_persistence"].append(sum(leg_returns) + len_pct)
        if have_prev_leg and abs(prev_leg_start - prev_leg_end) > 0.0:
            fib = (close - prev_leg_end) / (prev_leg_start - prev_leg_end)
            # mirror swing_fold_frame's degenerate-micro-leg guard: beyond the valid_range fib is undefined.
            out["fib_retracement"].append(fib if abs(fib) <= FIB_MAX_ABS else float("nan"))
        else:
            out["fib_retracement"].append(float("nan"))
        resolved = 0.0
        if len(leg_returns) >= 2 and direction != 0:
            max_prior_len = max(abs(x) for x in leg_returns)
            max_prior_steep = max(abs(x) for x in leg_steeps)
            persists = (len_pct > 0.0 and direction == 1) or (len_pct < 0.0 and direction == -1)
            if persists and abs(len_pct) > max_prior_len and abs(steep) > max_prior_steep:
                resolved = 1.0
        out["trend_resolved"].append(resolved)
    return out


def _cell_equal(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    a_missing = a is None or (isinstance(a, float) and not np.isfinite(a))
    b_missing = b is None or (isinstance(b, float) and not np.isfinite(b))
    if a_missing or b_missing:
        return a_missing and b_missing
    return abs(a - b) <= 1e-12 + tol * abs(b)


def test_swing_python_reference_pins_rust() -> None:
    """The Rust ``swing_fold`` equals the pure-Python reference fold cell-for-cell at every (symbol, minute)."""
    stream = _stream(n_sym=4, n_min=150)
    rust = swing_fold_frame(stream).sort(["symbol", "minute"])
    for symbol in sorted(stream["symbol"].unique().to_list()):
        sym = stream.filter(pl.col("symbol") == symbol).sort("minute")
        closes = sym["close"].to_list()
        epochs = [int(m.timestamp()) for m in sym["minute"].to_list()]
        ref = _python_swing(closes, epochs)
        got = rust.filter(pl.col("symbol") == symbol).sort("minute")
        for name in _FEATURES:
            got_col = got[name].to_list()
            for i, (gv, rv) in enumerate(zip(got_col, ref[name])):
                assert _cell_equal(gv, rv), f"{symbol}.{name}[{i}]: rust={gv} != python={rv}"


def test_swing_no_look_ahead() -> None:
    """THE property: the value at T over a buffer ending at T is identical whether or not bars after T exist.
    A pivot confirmed at T must use ONLY bars <= T — so growing the buffer past T never changes the row at T."""
    stream = _stream(n_sym=4, n_min=150)
    minutes = sorted(stream["minute"].unique())
    group = SwingGroup()
    # T sweeps the whole stream; for each T compare against buffers that include up to 30 extra future bars.
    for ti in range(2, len(minutes)):
        t_minute = minutes[ti]
        at_t = (
            group.compute(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= t_minute)}))
            .filter(pl.col("minute") == t_minute)
            .sort("symbol")
        )
        future_ti = min(ti + 30, len(minutes) - 1)
        with_future = (
            group.compute(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= minutes[future_ti])}))
            .filter(pl.col("minute") == t_minute)
            .sort("symbol")
        )
        for name in _FEATURES:
            a = at_t[name].to_list()
            b = with_future[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(a[sym_i], b[sym_i]), (
                    f"LOOK-AHEAD at T={ti} feature {name} sym {sym_i}: {a[sym_i]} (no future) != {b[sym_i]} (with future)"
                )


def test_swing_fold_equals_reseed() -> None:
    """KIND invariant: INCREMENTAL emission (re-seed over the buffer ending at each minute, take the last row)
    == the single BATCH backfill pass, cell-for-cell at every minute INCLUDING warmup. This is fold == reseed:
    the live path that re-seeds from the buffer each minute reaches the exact row the whole-history backfill
    produces — the property that makes live == backfill for the swing kind."""
    stream = _stream(n_sym=3, n_min=120)
    minutes = sorted(stream["minute"].unique())
    group = SwingGroup()
    batch = group.compute(BatchContext(frames={"minute_agg": stream}))
    for ti in range(len(minutes)):
        minute = minutes[ti]
        reseeded = (
            group.compute(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= minute)}))
            .filter(pl.col("minute") == minute)
            .sort("symbol")
        )
        batch_t = batch.filter(pl.col("minute") == minute).sort("symbol")
        for name in _FEATURES:
            a = reseeded[name].to_list()
            b = batch_t[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(a[sym_i], b[sym_i]), f"reseed!=batch @min{ti} {name} sym{sym_i}"


def test_swing_compute_latest_equals_backfill() -> None:
    """GROUP parity: compute_latest == compute().filter(last minute), cell-for-cell across the minute stream."""
    stream = _stream(n_sym=5, n_min=130)
    minutes = sorted(stream["minute"].unique())
    group = SwingGroup()
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))
    checkpoints = {1, 3, 10, 40, 80, len(minutes) - 1}
    for ti in [c for c in checkpoints if c < len(minutes)]:
        minute = minutes[ti]
        buffer = stream.filter(pl.col("minute") <= minute)
        latest = group.compute_latest(BatchContext(frames={"minute_agg": buffer})).sort("symbol")
        back_t = backfill.filter(pl.col("minute") == minute).sort("symbol")
        assert set(latest.columns) == set(back_t.columns)
        for name in _FEATURES:
            a = latest[name].to_list()
            b = back_t[name].to_list()
            for sym_i in range(len(a)):
                assert _cell_equal(a[sym_i], b[sym_i]), f"latest@min{ti} {name} sym{sym_i}"


def test_swing_fib_degenerate_microleg_guarded() -> None:
    """fib_retracement off a confirmed MICRO-leg (near-zero prior-leg range) explodes; the guard nulls any read
    beyond the declared valid_range so the column never ships an out-of-range degenerate value (seen LIVE to 450).
    This exact close path drives the raw kernel fib to ~13.5 (> FIB_MAX_ABS); the guarded frame must null it."""
    # up-dir established, a hair-thin up micro-leg to 100.02, then a deep drop -> tiny denom, explosive raw fib.
    closes = [100.0, 100.0, 100.8, 100.0, 100.02, 90.0, 90.0, 90.0]
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(len(closes))]
    frame = pl.DataFrame(
        {"symbol": ["S0"] * len(closes), "minute": minutes, "close": closes}
    ).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    out = swing_fold_frame(frame)
    fib = out["fib_retracement"]
    # the degenerate rows are nulled (not finite), and EVERY surviving value is within the declared range.
    assert fib.null_count() >= 1, "expected the explosive micro-leg fib to be nulled"
    finite = out.filter(pl.col("fib_retracement").is_not_null())["fib_retracement"]
    assert finite.len() >= 1 and float(finite.abs().max()) <= FIB_MAX_ABS, (
        f"fib_retracement must stay within +/-{FIB_MAX_ABS}; got max |{float(finite.abs().max())}|"
    )


def test_swing_stateful_compute_latest_equals_backfill_every_minute(monkeypatch) -> None:
    """STATEFUL LIVE == BACKFILL: with FP_SWING_STATEFUL=1, walking the carried per-symbol leg-state minute by
    minute (the production live driver — a monotonically growing/sliding buffer on ONE group instance, folding
    only the new minute each time) reaches the Rust whole-buffer backfill cell-for-cell at EVERY minute. This is
    the O(1)/minute path's value-identity proof — folding the new bar onto carried state == re-folding the whole
    buffer (fold == reseed)."""
    monkeypatch.setenv("FP_SWING_STATEFUL", "1")
    stream = _stream(n_sym=6, n_min=160, seed=7)
    minutes = sorted(stream["minute"].unique())
    backfill = SwingGroup().compute(BatchContext(frames={"minute_agg": stream}))
    # Simulate the live ring: a trailing buffer of at most RING_DEPTH minutes, advanced ONE minute at a time on a
    # single carried-state instance. A bounded ring (here shorter than the stream) also exercises that a symbol's
    # state is carried across minutes that have scrolled OUT of the buffer — the whole point of carrying state.
    ring_depth = 45
    group = SwingGroup()
    for ti in range(len(minutes)):
        minute = minutes[ti]
        lo = minutes[max(0, ti - ring_depth + 1)]
        buffer = stream.filter((pl.col("minute") >= lo) & (pl.col("minute") <= minute))
        latest = group.compute_latest(BatchContext(frames={"minute_agg": buffer})).sort("symbol")
        back_t = backfill.filter(pl.col("minute") == minute).sort("symbol")
        for name in _FEATURES:
            got = latest[name].to_list()
            want = back_t[name].to_list()
            assert len(got) == len(want), f"row count @min{ti} {name}"
            for sym_i in range(len(got)):
                assert _cell_equal(got[sym_i], want[sym_i]), (
                    f"STATEFUL latest@min{ti} {name} sym{sym_i}: {got[sym_i]} != backfill {want[sym_i]}"
                )


def test_swing_stateful_handles_redelivered_minute(monkeypatch) -> None:
    """A re-delivered minute (reconnect/replay of the SAME bar) is folded at most once: feeding the buffer twice
    in a row leaves the carried state — and the emitted row — identical to a single feed (keep-last de-dup)."""
    monkeypatch.setenv("FP_SWING_STATEFUL", "1")
    stream = _stream(n_sym=4, n_min=90, seed=3)
    minutes = sorted(stream["minute"].unique())
    backfill = SwingGroup().compute(BatchContext(frames={"minute_agg": stream}))
    group = SwingGroup()
    for ti in range(len(minutes)):
        buffer = stream.filter(pl.col("minute") <= minutes[ti])
        ctx = BatchContext(frames={"minute_agg": buffer})
        group.compute_latest(ctx)  # first delivery
        latest = group.compute_latest(ctx).sort("symbol")  # SAME minute re-delivered -> must be a no-op fold
        back_t = backfill.filter(pl.col("minute") == minutes[ti]).sort("symbol")
        for name in _FEATURES:
            got, want = latest[name].to_list(), back_t[name].to_list()
            for sym_i in range(len(got)):
                assert _cell_equal(got[sym_i], want[sym_i]), f"redelivered@min{ti} {name} sym{sym_i}"


def test_swing_stateful_reseeds_on_rewound_buffer(monkeypatch) -> None:
    """A buffer that REWINDS (a fresh/replayed history ending BEFORE the carried state's last minute) drops the
    stale state and reseeds from the buffer, so the emitted row matches the backfill at the rewound minute — the
    carried-state path is correct regardless of call order (crash-recovery / reconnect-from-earlier safe)."""
    monkeypatch.setenv("FP_SWING_STATEFUL", "1")
    stream = _stream(n_sym=3, n_min=120, seed=9)
    minutes = sorted(stream["minute"].unique())
    backfill = SwingGroup().compute(BatchContext(frames={"minute_agg": stream}))
    group = SwingGroup()
    # advance to a late minute, then HAND IT an earlier buffer (rewind) and assert it reseeds to the right row.
    group.compute_latest(BatchContext(frames={"minute_agg": stream.filter(pl.col("minute") <= minutes[100])}))
    rewound_ti = 30
    rewound = stream.filter(pl.col("minute") <= minutes[rewound_ti])
    latest = group.compute_latest(BatchContext(frames={"minute_agg": rewound})).sort("symbol")
    back_t = backfill.filter(pl.col("minute") == minutes[rewound_ti]).sort("symbol")
    for name in _FEATURES:
        got, want = latest[name].to_list(), back_t[name].to_list()
        for sym_i in range(len(got)):
            assert _cell_equal(got[sym_i], want[sym_i]), f"rewound@min{rewound_ti} {name} sym{sym_i}"


def test_swing_features_are_valid() -> None:
    """Every declared swing feature is VALID (>= 2 unique finite values across the stream) — no dead feature."""
    stream = _stream(n_sym=6, n_min=200, seed=5)
    out = swing_fold_frame(stream)
    for name in _FEATURES:
        finite = out.filter(pl.col(name).is_not_null() & pl.col(name).is_finite())[name]
        assert finite.n_unique() >= 2, f"{name}: not VALID (only {finite.n_unique()} unique finite values)"
