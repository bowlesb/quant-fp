"""The one feature engine — carried per-symbol state, one path, no per-minute polars framework.

Replaces the legacy machinery sprawl (two engines + four ``step*`` twins + per-kind state wrappers + the
duplicate capture paths + the byte-parity demolition gate) with a single understandable engine built on Ben's
``tracker.py`` model: a per-symbol carried numpy buffer, a few array ops per bar, and one read surface.

THE ONE PATH
------------
    engine = CleanEngine(groups, symbols, window)  # fixed symbol index + the deepest window any group reads
    engine.seed(history)                           # replay warm-up history bar-by-bar to build carried state
    feats = engine.step(minute_bars)               # fold ONE minute, then read every group's features for it

There is no second "fast vs backfill" formulation and no parity gate between them, because there is only one
computation: **the live step and the backfill are the same replay.** Backfill = ``seed`` the buffer then
``step`` each historical minute; live = ``step`` each new minute. They cannot diverge — it is the same code.
(This is the ``seed(H); fold(m) == seed(H+m)`` invariant the old design proved as an *endpoint*; here it is
simply how the engine works.)

THE STATE
---------
``RingBuffer`` carries, per symbol, the last ``window`` minutes of bar columns as a fixed ``(n_symbols,
window, n_cols)`` numpy array with a per-symbol write cursor (a circular buffer — append is O(1), a read of
the trailing window is a roll). Absent symbols simply do not advance their cursor, so each symbol's buffer
holds its own present bars in order — the positional history a windowed feature reads, gap-safe by
construction. This is ``tracker.py``'s ``CircularBufferOnDisk`` generalized to the symbol axis, in memory.

THE GROUP INTERFACE
-------------------
Every feature group implements ONE method against the carried window:

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        '''Return {feature_name: (n_symbols,) array} for the latest minute, computed from the carried
        per-symbol window. Numpy in, numpy out — no polars.'''

``Window`` is the read surface over the carried state: ``window.trailing("close")`` returns the
``(n_symbols, window)`` trailing matrix (oldest→newest); ``window.latest("close")`` the ``(n_symbols,)``
current bar; ``window.count()`` the per-symbol filled length (warm-up / readiness). The math each group
already had (rolling sums, OLS, candlestick patterns, …) becomes a small numpy function over these arrays —
the same arithmetic, expressed once, framework-free.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class EngineGroup(Protocol):
    """The one interface a feature group implements to run on the engine. ``name`` + ``feature_names`` are the
    output contract; ``input_cols`` are the bar columns the group reads; ``compute`` is its numpy math over the
    carried window. (Class-A intraday-invariant groups — calendar / sector / daily snapshots — implement the
    same ``compute`` but read once-per-session state instead of the rolling window.)"""

    name: str
    feature_names: tuple[str, ...]
    input_cols: tuple[str, ...]

    def compute(self, window: "Window") -> dict[str, np.ndarray]: ...


class RingBuffer:
    """Per-symbol circular buffer of the last ``window`` minutes of bar columns — the carried state.

    State is one ``(n_symbols, window, n_cols)`` float array + a per-symbol write cursor and filled-count.
    ``append`` writes the present symbols' bars at their next slot (O(1) per symbol); ``trailing`` returns each
    symbol's window in time order. Absent symbols are untouched (cursor unchanged), so a gap reads the last
    *present* bars — the positional window a feature wants, gap-safe by construction (the tracker.py model).
    """

    def __init__(self, symbols: list[str], window: int, cols: tuple[str, ...]) -> None:
        self.symbols = list(symbols)
        self.index = {s: i for i, s in enumerate(self.symbols)}
        self.n = len(self.symbols)
        self.window = int(window)
        self.cols = tuple(cols)
        self.col_index = {c: i for i, c in enumerate(self.cols)}
        self._buf = np.full((self.n, self.window, len(self.cols)), np.nan, dtype=np.float64)
        self._write = np.zeros(self.n, dtype=np.int64)  # next slot to write, per symbol
        self._count = np.zeros(self.n, dtype=np.int64)  # filled minutes so far, per symbol (caps at window)

    def append(self, rows: np.ndarray, present_pos: np.ndarray) -> None:
        """Write this minute's bars: ``rows`` is ``(n_present, n_cols)`` for the present symbols at index
        positions ``present_pos``. Each present symbol advances its own cursor by one (mod window); absent
        symbols are untouched (their cursor/count hold, so their window keeps its last present bars)."""
        if present_pos.size == 0:
            return
        slots = self._write[present_pos]
        self._buf[present_pos, slots, :] = rows
        self._write[present_pos] = (slots + 1) % self.window
        self._count[present_pos] = np.minimum(self._count[present_pos] + 1, self.window)

    def trailing(self, col: str) -> np.ndarray:
        """The ``(n_symbols, window)`` trailing matrix for ``col``, oldest→newest per symbol (a per-symbol roll
        so the newest bar is the last column). NaN where a symbol has fewer than ``window`` present bars."""
        ci = self.col_index[col]
        plane = self._buf[:, :, ci]  # (n, window) in physical slot order
        # roll each symbol so the column after its write cursor (the oldest) is first, newest is last.
        idx = (self._write[:, None] + np.arange(self.window)[None, :]) % self.window
        return np.take_along_axis(plane, idx, axis=1)

    def latest(self, col: str) -> np.ndarray:
        """The ``(n_symbols,)`` current (newest) bar value for ``col``. NaN for a symbol with no bars yet."""
        ci = self.col_index[col]
        newest = (self._write - 1) % self.window
        val = self._buf[np.arange(self.n), newest, ci]
        return np.where(self._count > 0, val, np.nan)

    def count(self) -> np.ndarray:
        """Per-symbol filled-minute count (for warm-up / readiness — a feature whose window isn't filled yet
        returns NaN by its own math, exactly as a short backfill window does)."""
        return self._count.copy()


class Window:
    """The read surface a group's ``compute`` sees — a thin view over the ``RingBuffer`` (+ optional
    per-session state for Class-A groups). Keeps the group math decoupled from the buffer internals."""

    def __init__(
        self,
        ring: RingBuffer,
        state: dict[str, np.ndarray] | None = None,
        static: dict[str, np.ndarray] | None = None,
        minute_epoch: int = 0,
        session: dict[str, np.ndarray] | None = None,
        present: np.ndarray | None = None,
    ) -> None:
        self._ring = ring
        # The REAL per-symbol current-minute delivery mask (the engine's ``present_pos`` at step time). A
        # presence-gated group (EMA decay, cross-sectional count, cumulative increment) MUST gate on THIS, not on
        # ``isfinite(latest(col))`` — because ``latest`` returns the CARRIED value for an absent symbol, so
        # isfinite is True even when the symbol delivered no bar this minute. Using the carried value as
        # "present" is the systemic bug (an absent EMA decays, an absent symbol gets counted, a cumulative count
        # increments) — ``present`` is the one correct source of "did this symbol deliver a bar this minute".
        self._present = present if present is not None else np.ones(ring.n, dtype=bool)
        self.symbols = ring.symbols
        self.n = ring.n
        # ``state`` is the group's OWN carried per-symbol state (mutated in place across steps): the engine holds
        # one dict per group and hands it back each minute. This is how the "fork" kinds that a windowed ring
        # can't express live on the one spine — an EMA group keeps its decayed value, a cumulative group its
        # session running sum, a swing group its leg-state — each a small per-symbol array the group reads and
        # updates in ``compute``. The engine owns the lifecycle (creation, seed-replay, hand-back); the group
        # owns the math. (Empty for a pure windowed group, which reads only the ring.)
        self.state = state if state is not None else {}
        # Static per-symbol labels that never change intraday (sector id, the symbol index itself) — for the
        # cross-sectional groups that reduce/group over the symbol axis (breadth by sector, sector_beta).
        self.static = static or {}
        self.minute_epoch = minute_epoch  # for session-reset / time-of-day groups (cumulative, seasonality)
        # ``session`` is the engine's per-session memo for the DAILY-SNAPSHOT (Class-A intraday-invariant)
        # groups — prior-day levels, the sector map — computed once per session and broadcast. The engine
        # populates it at the session boundary; a snapshot group reads it instead of the rolling window.
        self.session = session if session is not None else {}

    def trailing(self, col: str) -> np.ndarray:
        return self._ring.trailing(col)

    def latest(self, col: str) -> np.ndarray:
        return self._ring.latest(col)

    def count(self) -> np.ndarray:
        return self._ring.count()

    def present(self) -> np.ndarray:
        """The ``(n_symbols,)`` bool mask of which symbols DELIVERED a bar THIS minute. The one correct presence
        source for a presence-gated group — NOT ``isfinite(latest(col))``, which is True on an absent symbol
        because ``latest`` returns the carried value. EMA decay, cross-sectional counts, and cumulative
        increments must gate on this."""
        return self._present


class CleanEngine:
    """The one engine: a fixed symbol index + a carried ``RingBuffer`` + the groups, with one ``step`` that
    folds a minute and reads every group's features. No second form, no parity gate, no per-minute polars."""

    def __init__(
        self,
        groups: list[EngineGroup],
        symbols: list[str],
        window: int,
        static: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.groups = list(groups)
        self.symbols = list(symbols)
        cols: list[str] = []
        for group in self.groups:
            for col in group.input_cols:
                if col not in cols:
                    cols.append(col)
        self.cols = tuple(cols)
        self.ring = RingBuffer(self.symbols, window, self.cols)
        # One carried-state dict per group, owned by the engine and handed back each minute. A group's
        # ``compute`` reads + mutates its own ``window.state`` (its EMA value / session running sum / swing
        # leg-state); a pure windowed group leaves it empty. Created lazily so a group declares no state-machine
        # boilerplate — it just writes into the dict the first time it needs to.
        self._group_state: dict[str, dict[str, np.ndarray]] = {g.name: {} for g in self.groups}
        # Static per-symbol labels for the cross-sectional groups (sector id, etc.), constant intraday.
        self.static = static or {}
        # The per-session memo the daily-snapshot groups read (prior-day levels, sector map) — populated once
        # per session via ``set_session``; a snapshot group reads ``window.session`` instead of the ring.
        self.session: dict[str, np.ndarray] = {}
        # The absorbed-minute watermark (idempotency, the engine owns it ONCE for every carried-state kind): the
        # latest minute_epoch already folded. A re-delivered or out-of-order minute (epoch <= watermark) is a
        # NO-OP — it does not re-append the bar or re-advance any EMA / cumulative / swing state. Without this a
        # duplicate minute double-counts the carried sums (the C4 footgun). 0 = nothing absorbed yet.
        self._watermark = 0
        self._last_out: dict[str, dict[str, np.ndarray]] = (
            {}
        )  # cached output, returned on a stale re-delivery

    def set_session(self, session: dict[str, np.ndarray]) -> None:
        """Set the per-session snapshot memo (prior-day levels, etc.) — called once at each session boundary.
        Daily-snapshot groups read it via ``window.session``; everything else ignores it."""
        self.session = dict(session)

    def _marshal(self, minute_bars: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """Turn the current minute's bars (``{col: (n_present,) array}`` + a ``symbol`` array) into the
        ``(n_present, n_cols)`` row block + the index positions to scatter into. Pure numpy — no sort, no
        frame: the present symbols are read by name into their fixed index slots."""
        syms = minute_bars["symbol"]
        pos = np.array([self.ring.index[s] for s in syms], dtype=np.int64)
        rows = np.column_stack([np.asarray(minute_bars[c], dtype=np.float64) for c in self.cols])
        return rows, pos

    def step(self, minute_bars: dict[str, np.ndarray]) -> dict[str, dict[str, np.ndarray]]:
        """Fold ONE minute into the carried buffer, then read every group's features for it. Each group sees its
        OWN carried state + the static labels; a fork-kind group (EMA / cumulative / swing) reads and updates
        ``window.state`` in place. Returns ``{group_name: {feature_name: (n_symbols,) array}}``."""
        minute_epoch = (
            int(np.asarray(minute_bars["minute_epoch"]).flat[0]) if "minute_epoch" in minute_bars else 0
        )
        # Idempotency (the engine owns it once for every carried-state kind): a re-delivered / stale minute
        # (epoch <= the watermark) is a NO-OP. We do NOT re-append the bar and do NOT call any group's
        # ``compute`` — because a fork-kind ``compute`` mutates its carried state in place, calling it twice for
        # the same minute would double-advance the EMA / cumulative sum / swing leg (the C4 footgun). Instead we
        # return the cached output from when that minute was first folded. (No minute_epoch supplied — the
        # simple/test path — folds every step.)
        if minute_epoch and minute_epoch <= self._watermark:
            return self._last_out
        rows, pos = self._marshal(minute_bars)
        self.ring.append(rows, pos)
        if minute_epoch:
            self._watermark = minute_epoch
        present = np.zeros(self.ring.n, dtype=bool)  # the REAL delivery mask for this minute
        present[pos] = True
        out: dict[str, dict[str, np.ndarray]] = {}
        for group in self.groups:
            window = Window(
                self.ring,
                self._group_state[group.name],
                self.static,
                minute_epoch,
                self.session,
                present,
            )
            out[group.name] = group.compute(window)
        self._last_out = out
        return out

    def seed(self, history: list[dict[str, np.ndarray]]) -> None:
        """Replay warm-up history minute-by-minute (== backfill over the buffer): fold each historical minute AND
        run every group's ``compute`` so the carried state — the ring AND each fork-kind group's EMA / cumulative
        / swing state — is exactly what it would be having processed that history. This is just ``step`` with the
        output discarded; live state == backfill state by construction (it is the same replay)."""
        for minute_bars in history:
            self.step(minute_bars)
