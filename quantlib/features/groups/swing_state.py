"""Carried per-symbol leg-state for the SWING / ZigZag fold — the O(1)-per-minute live twin of the
``quant_tick.swing_fold`` whole-buffer backfill kernel.

The Rust ``swing_fold`` kernel folds each symbol's close series bar-by-bar through a tiny per-symbol state
machine (direction, leg start, running extreme, bounded leg ring) and emits one point-in-time row per bar.
Its per-bar work is O(1); the cost in the LIVE path was that the group re-ran the WHOLE-buffer fold every
minute (``FeatureGroup.compute_latest`` default = ``compute()`` then drop all but the last row), so a 245-bar
ring re-folded 245 bars each minute to keep 1 row.

``SwingState`` carries that per-symbol leg-state BETWEEN minutes: ``seed`` it once from the trailing buffer,
then ``fold`` only the new minute's bars (O(symbols) per minute, not O(symbols × window)). The arithmetic is
byte-for-byte the same scalar ops the Rust kernel runs (and the pure-Python reference in tests/test_fp_swing.py
already pins that reference == Rust cell-for-cell), so the emitted row equals the Rust backfill at every cell.

PARITY (the kind invariant the whole platform rests on): ``seed(H); fold(m)`` reaches the SAME state — hence
the SAME emitted row — as ``seed(H + m)`` (re-seeding with the minute appended). The fold is a deterministic
single ordered pass per symbol, so folding one more bar onto the carried state is identical to replaying the
buffer with that bar appended — which is exactly what the backfill whole-buffer fold does. tests assert this
against the Rust kernel directly (the live path is guarded == the Rust backfill, cell-for-cell).
"""
from __future__ import annotations

from collections import deque

THETA: float = 0.005
RING_K: int = 8
DAY_SECS: int = 86_400
FIB_MAX_ABS: float = 10.0

_FEATURE_COLS: tuple[str, ...] = (
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


class _SymbolLeg:
    """One symbol's running leg-state — the exact fields the Rust kernel resets at each symbol block. Holds
    no history beyond the bounded ``ring_k`` leg deques; advancing it one bar is O(1)."""

    __slots__ = (
        "direction",
        "leg_start_price",
        "leg_start_min",
        "extreme",
        "extreme_min",
        "hi",
        "hi_min",
        "lo",
        "lo_min",
        "prev_leg_start",
        "prev_leg_end",
        "have_prev_leg",
        "n_pivots_today",
        "cur_day",
        "leg_returns",
        "leg_steeps",
        "n_alternations",
        "last_min",
        "last_row",
    )

    def __init__(self) -> None:
        self.direction: int = 0
        self.leg_start_price: float = float("nan")
        self.leg_start_min: int = 0
        self.extreme: float = float("nan")
        self.extreme_min: int = 0
        self.hi: float = float("nan")
        self.hi_min: int = 0
        self.lo: float = float("nan")
        self.lo_min: int = 0
        self.prev_leg_start: float = float("nan")
        self.prev_leg_end: float = float("nan")
        self.have_prev_leg: bool = False
        self.n_pivots_today: float = 0.0
        self.cur_day: int | None = None
        self.leg_returns: deque[float] = deque()
        self.leg_steeps: deque[float] = deque()
        self.n_alternations: float = 0.0
        # The last minute (epoch seconds) absorbed into this state — guards against re-folding a re-delivered
        # bar (reconnect/replay) so a minute is folded AT MOST once (keep-last semantics of the live ring).
        self.last_min: int = -(2**62)
        # The row emitted at ``last_min`` — re-served when this minute brought no NEW bar for the symbol so the
        # group still reports the symbol's standing point-in-time state (the same row the whole-buffer fold
        # would emit at ``last_min``, since no later bar exists to change it).
        self.last_row: tuple[float, ...] | None = None

    def _push_pivot(self, pivot_price: float, start_price: float, span_secs: int) -> None:
        signed_ret = (pivot_price - start_price) / start_price if start_price > 0.0 else 0.0
        mins = span_secs // 60
        steep = signed_ret / mins if mins > 0 else 0.0
        self.prev_leg_start = start_price
        self.prev_leg_end = pivot_price
        self.have_prev_leg = True
        self.leg_returns.append(signed_ret)
        self.leg_steeps.append(steep)
        while len(self.leg_returns) > RING_K:
            self.leg_returns.popleft()
        while len(self.leg_steeps) > RING_K:
            self.leg_steeps.popleft()

    def _reset_for_new_session(self) -> None:
        """Drop the entire leg-state so the next bar bootstraps a FRESH leg — the session-boundary rule.

        WHY RESET (not carry across the overnight gap): the production BACKFILL source of truth materializes
        swing PER DAY (``load_raw_minute_agg(day)`` — one session's bars), so its fold bootstraps a fresh leg at
        each session's first bar and NEVER carries the leg across the overnight gap. The held live state must do
        the SAME or it diverges from backfill the moment a live run spans two sessions (a continuous capture, or
        a buffer that straddles the boundary). Measured: per-day backfill vs a whole-buffer cross-day carry
        disagree on swing_dir / n_alternations / persistence at the new session's open. The overnight gap also
        distorts the leg geometry (a 30% gap is not a real intraday swing), so the reset is correct on its own
        merits. Only ``cur_day`` is preserved (set by the caller) — everything else returns to the cold state.
        """
        self.direction = 0
        self.leg_start_price = float("nan")
        self.leg_start_min = 0
        self.extreme = float("nan")
        self.extreme_min = 0
        self.hi = float("nan")
        self.hi_min = 0
        self.lo = float("nan")
        self.lo_min = 0
        self.prev_leg_start = float("nan")
        self.prev_leg_end = float("nan")
        self.have_prev_leg = False
        self.n_pivots_today = 0.0
        self.leg_returns.clear()
        self.leg_steeps.clear()
        self.n_alternations = 0.0

    def advance(self, close: float, minute: int) -> tuple[float, ...]:
        """Fold ONE bar into the state and return its point-in-time row (the 9 features in ``_FEATURE_COLS``
        order). Mirrors the per-bar branch + emit of the per-day backfill fold exactly."""
        day = minute // DAY_SECS
        if self.cur_day is not None and day != self.cur_day:
            # SESSION BOUNDARY: reset the whole leg (match per-day backfill — see ``_reset_for_new_session``).
            self._reset_for_new_session()
        self.cur_day = day

        if self.leg_start_price != self.leg_start_price:  # nan -> first bar of the (re)started session block
            self.leg_start_price = close
            self.leg_start_min = minute
            self.extreme = close
            self.extreme_min = minute
            self.hi = self.lo = close
            self.hi_min = self.lo_min = minute
        elif self.direction == 0:
            if close > self.hi:
                self.hi, self.hi_min = close, minute
            if close < self.lo:
                self.lo, self.lo_min = close, minute
            down_rev = (self.hi - close) / self.hi if self.hi > 0.0 else 0.0
            up_rev = (close - self.lo) / self.lo if self.lo > 0.0 else 0.0
            if down_rev >= THETA and down_rev >= up_rev:
                self._push_pivot(self.hi, self.leg_start_price, self.hi_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = -1
                self.leg_start_price, self.leg_start_min = self.hi, self.hi_min
                self.extreme, self.extreme_min = close, minute
            elif up_rev >= THETA:
                self._push_pivot(self.lo, self.leg_start_price, self.lo_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = 1
                self.leg_start_price, self.leg_start_min = self.lo, self.lo_min
                self.extreme, self.extreme_min = close, minute
        elif self.direction == 1:
            if close >= self.extreme:
                self.extreme, self.extreme_min = close, minute
            elif self.extreme > 0.0 and (self.extreme - close) / self.extreme >= THETA:
                self._push_pivot(self.extreme, self.leg_start_price, self.extreme_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = -1
                self.leg_start_price, self.leg_start_min = self.extreme, self.extreme_min
                self.extreme, self.extreme_min = close, minute
        else:  # direction == -1
            if close <= self.extreme:
                self.extreme, self.extreme_min = close, minute
            elif self.extreme > 0.0 and (close - self.extreme) / self.extreme >= THETA:
                self._push_pivot(self.extreme, self.leg_start_price, self.extreme_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = 1
                self.leg_start_price, self.leg_start_min = self.extreme, self.extreme_min
                self.extreme, self.extreme_min = close, minute

        len_pct = (
            (close - self.leg_start_price) / self.leg_start_price if self.leg_start_price > 0.0 else 0.0
        )
        mins = (minute - self.leg_start_min) // 60
        steep = len_pct / mins if mins > 0 else 0.0
        msp = float(mins) if self.direction != 0 else float("nan")
        persistence = sum(self.leg_returns) + len_pct
        if self.have_prev_leg and abs(self.prev_leg_start - self.prev_leg_end) > 0.0:
            fib = (close - self.prev_leg_end) / (self.prev_leg_start - self.prev_leg_end)
            fib = fib if abs(fib) <= FIB_MAX_ABS else float("nan")
        else:
            fib = float("nan")
        resolved = 0.0
        if len(self.leg_returns) >= 2 and self.direction != 0:
            max_prior_len = max(abs(x) for x in self.leg_returns)
            max_prior_steep = max(abs(x) for x in self.leg_steeps)
            persists = (len_pct > 0.0 and self.direction == 1) or (len_pct < 0.0 and self.direction == -1)
            if persists and abs(len_pct) > max_prior_len and abs(steep) > max_prior_steep:
                resolved = 1.0
        self.last_min = minute
        row = (
            float(self.direction),
            steep,
            len_pct,
            msp,
            self.n_pivots_today,
            self.n_alternations,
            persistence,
            fib,
            resolved,
        )
        self.last_row = row
        return row


class SwingState:
    """The live carried state for the whole shard: one ``_SymbolLeg`` per symbol. Each ``step`` folds only the
    bars a symbol has NOT yet absorbed and returns the latest row for the symbols present at the buffer's latest
    minute — O(symbols × new-bars) per minute, not O(symbols × window).

    A bar is folded AT MOST once (``_SymbolLeg.last_min`` guards re-delivered minutes from the live ring's
    keep-last de-dup). A symbol seen for the first time is folded from the start of whatever buffer it appears
    in — identical to the Rust kernel, whose per-symbol block likewise starts at the first bar it sees."""

    def __init__(self) -> None:
        self._legs: dict[str, _SymbolLeg] = {}

    def min_absorbed(self, symbols: list[str]) -> int | None:
        """The smallest ``last_min`` across ``symbols`` that already have carried state, or None if ANY of them
        is brand-new (no state yet). A None forces the caller to fold the WHOLE buffer (a new symbol must seed
        from its first bar); otherwise the caller may slice the buffer to ``minute > min_absorbed`` — every bar
        a known symbol has not yet absorbed is strictly newer than the MIN, so the slice loses nothing while
        marshaling only the unabsorbed tail (the O(new-bars) live cost instead of O(window))."""
        floor: int | None = None
        for symbol in symbols:
            leg = self._legs.get(symbol)
            if leg is None:
                return None
            floor = leg.last_min if floor is None else min(floor, leg.last_min)
        return floor

    def standing_row(self, symbol: str) -> tuple[float, ...] | None:
        """The symbol's last emitted point-in-time row without folding (for a re-delivered minute that brought no
        new bar). None if the symbol has no carried state yet."""
        leg = self._legs.get(symbol)
        return None if leg is None else leg.last_row

    def fold_symbol_to(
        self, symbol: str, closes: list[float], minutes: list[int]
    ) -> tuple[float, ...] | None:
        """Advance ``symbol``'s carried state through ``(closes, minutes)`` (sorted ascending), folding only
        bars newer than what it already absorbed, and return its current point-in-time row. Returns None only
        for an empty series with no prior state.

        REWIND-SAFE RESEED: if the buffer's NEWEST minute for the symbol is older than the state's ``last_min``,
        the buffer was replayed/rewound (a reconnect from an earlier point, a fresh history shorter than what we
        held, or a restart). The carried state cannot be trusted past the buffer's end, so it is reseeded from
        scratch over THIS buffer — which, being a single ordered pass, reaches exactly the row the whole-buffer
        backfill fold emits at that minute (fold == reseed). This makes a single ``compute_latest`` call on ANY
        buffer correct regardless of call order, so the generic live==backfill gate holds even off-sequence.
        """
        leg = self._legs.get(symbol)
        if leg is not None and minutes and minutes[-1] < leg.last_min:
            leg = None  # rewound buffer -> drop the stale carried state and reseed below
        if leg is None:
            leg = _SymbolLeg()
            self._legs[symbol] = leg
        for close, minute in zip(closes, minutes):
            if minute <= leg.last_min:
                continue  # already absorbed (re-delivered minute) — keep-last, never double-fold
            leg.advance(close, minute)
        return leg.last_row
