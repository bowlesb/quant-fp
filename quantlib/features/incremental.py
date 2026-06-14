"""Incremental windowed-sum state — the pre-prepped-between-minutes path to a minimal-compute minute mark.

Instead of re-scanning the whole trailing buffer every minute (the current 86%-shuffling cost), keep a
running per-(symbol, window) sum of each value column and, when a new minute arrives, ADD the new minute and
EXPIRE the minutes that just fell out of each window — O(symbols × windows × cols) ≈ a few ms, vs O(buffer ×
…). This is the same windowed sum the Rust kernel computes, so the declarative groups assemble from these
sums unchanged.

Window semantics match ``quant_tick.windowed_sums`` exactly: window ``w`` covers minutes with epoch in
``(T − w·60, T]`` (a minute at exactly ``T − w`` is excluded).

PARITY: these running sums must equal the batch recompute within tolerance (guarded by
tests/test_fp_incremental.py). Incremental float sums drift slowly; bound it by re-seeding from the buffer
each session (``seed``), which also gives crash recovery. Backfill stays the polars rolling form (truth);
live uses this; the parity test proves they agree.

POC scope: a fixed, symbol-aligned value matrix per minute (the production form will index symbols that come
and go). The accumulator is value-column agnostic — the caller passes whatever derived columns it needs.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.declarative import ReductionGroup, assemble_from_long, build_plan


class WindowedSumState:
    """Running per-(window, symbol, col) sums, updated one minute at a time. ``values`` passed to ``update``
    is an ``(n_symbols, n_cols)`` float matrix (nulls already filled to 0), symbol-aligned to ``symbols``."""

    def __init__(self, symbols: list[str], windows: tuple[int, ...], n_cols: int) -> None:
        self.symbols = list(symbols)
        self.n = len(self.symbols)
        self.windows = tuple(int(w) for w in windows)
        self.n_cols = n_cols
        self.running = np.zeros((len(self.windows), self.n, n_cols), dtype=np.float64)
        self._buf_epoch: list[int] = []
        self._buf_vals: list[np.ndarray] = []
        self._oldest = [0] * len(self.windows)  # per-window index of the oldest minute still in the sum

    def update(self, minute_epoch: int, values: np.ndarray) -> None:
        """Fold one new minute into every window's running sum, then expire minutes now outside each window."""
        index = len(self._buf_epoch)
        self._buf_epoch.append(int(minute_epoch))
        self._buf_vals.append(values)
        for wi, w in enumerate(self.windows):
            self.running[wi] += values  # the new minute is in every window (epoch == T > T - w)
            cutoff = minute_epoch - w * 60
            oldest = self._oldest[wi]
            while oldest <= index and self._buf_epoch[oldest] <= cutoff:  # minute at/under T-w left the window
                self.running[wi] -= self._buf_vals[oldest]
                oldest += 1
            self._oldest[wi] = oldest

    def trim(self) -> None:
        """Drop buffered minutes older than the longest window (bound memory). Call after each update."""
        if not self._buf_epoch:
            return
        keep_from = min(self._oldest)
        if keep_from:
            self._buf_epoch = self._buf_epoch[keep_from:]
            self._buf_vals = self._buf_vals[keep_from:]
            self._oldest = [o - keep_from for o in self._oldest]

    def sums(self, window: int) -> np.ndarray:
        """The current ``(n_symbols, n_cols)`` running sum for ``window`` (minutes)."""
        return self.running[self.windows.index(window)]


class IncrementalEngine:
    """The live incremental execution path for the declarative reduction groups. Holds a per-shard
    ``WindowedSumState`` over the union of all groups' value columns (built by ``build_plan`` — the SAME
    columns the batch sums), and at each minute derives ONLY the new minute's values, folds them in, and
    assembles features from the running sums via ``assemble_from_long`` (the SAME core as the batch). So the
    feature logic is identical to the batch/backfill paths; only the source of the sums differs.

    Usage: ``seed(buffer_frame)`` once (replays the buffer to establish state + symbols), then ``step(frame)``
    each minute (``frame`` = the trailing buffer including the new latest minute) -> {group: feature_frame}.
    """

    DERIVE_SLICE = 6  # minutes of history needed to derive a minute's value columns (max intra-value lag + slack)

    def __init__(self, groups: list[ReductionGroup]) -> None:
        self.groups = [g for g in groups if isinstance(g, ReductionGroup)]
        self.derived, self.extra, self.value_cols, self.plan, self.reg_plan, self.windows = build_plan(self.groups)
        self.reduce_input = self.groups[0].reduce_input if self.groups else "minute_agg"
        input_cols: list[str] = []
        for group in self.groups:
            for col in group._input_columns():
                if col not in input_cols:
                    input_cols.append(col)
        self.input_cols = input_cols
        self.symbols: list[str] | None = None
        self.state: WindowedSumState | None = None

    def _derive(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Apply the union value-column derivation (same exprs the batch uses) to a frame."""
        return (
            frame.select(self.input_cols).sort(["symbol", "minute"]).with_columns(self.derived).with_columns(self.extra)
        )

    def _matrix_at(self, derived_frame: pl.DataFrame, minute: object) -> np.ndarray:
        """The (n_symbols, n_value_cols) value matrix for ``minute``, symbol-aligned to ``self.symbols``
        (nulls -> 0, matching the kernel). Asserts a fixed symbol set (V1)."""
        rows = derived_frame.filter(pl.col("minute") == minute).sort("symbol")
        if rows.height != len(self.symbols or []):
            raise ValueError(f"incremental: symbol set changed ({rows.height} != {len(self.symbols or [])}); re-seed")
        return rows.select(self.value_cols).fill_null(0.0).to_numpy()

    def seed(self, buffer_frame: pl.DataFrame) -> None:
        """Establish the symbol set and fold every buffered minute into fresh state (== the batch recompute
        over the buffer; also the daily-resync / crash-recovery entry point)."""
        self.symbols = sorted(buffer_frame["symbol"].unique().to_list())
        self.state = WindowedSumState(self.symbols, self.windows, len(self.value_cols))
        derived_frame = self._derive(buffer_frame)
        for minute in sorted(derived_frame["minute"].unique()):
            self.state.update(int(minute.timestamp()), self._matrix_at(derived_frame, minute))
            self.state.trim()

    def step(self, frame: pl.DataFrame) -> dict[str, pl.DataFrame]:
        """Fold the new latest minute and assemble features from the running sums. ``frame`` is the trailing
        buffer including the new minute. Seeds lazily on first call."""
        latest = frame["minute"].max()
        if self.state is None:
            self.seed(frame)
        else:
            # V1 derives the new minute's value columns over the WHOLE buffer (same as the batch) — necessary
            # for columns that depend on long history (e.g. OBV = cum_sum(signed), and the frame-relative
            # centered-time origin), so the derived value matches the batch exactly. V2 optimization: derive
            # the cheap short-lag columns over a small slice and maintain the few cumulative ones (OBV) as
            # running per-symbol state — that removes the last O(buffer) cost from the minute mark.
            derived_frame = self._derive(frame)
            self.state.update(int(latest.timestamp()), self._matrix_at(derived_frame, latest))
            self.state.trim()
        long = self._running_long()
        latest_frame = frame.filter(pl.col("minute") == latest)
        return assemble_from_long(self.groups, long, latest_frame, latest, self.plan, self.reg_plan)

    def _running_long(self) -> pl.DataFrame:
        """The running sums as a LONG (symbol, window, <value-col sum>) frame — the exact shape
        ``assemble_from_long`` expects, so the SAME assemble code runs as in the batch (NO pivot to build it)."""
        running = self.state.running  # (n_windows, n_symbols, n_cols)
        n_win, n_sym, _ = running.shape
        data: dict[str, object] = {
            "symbol": self.symbols * n_win,
            "window": [w for w in self.windows for _ in range(n_sym)],
        }
        for col_index, col in enumerate(self.value_cols):
            data[col] = running[:, :, col_index].reshape(-1)
        return pl.DataFrame(data)
