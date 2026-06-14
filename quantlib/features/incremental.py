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

import numpy as np


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
