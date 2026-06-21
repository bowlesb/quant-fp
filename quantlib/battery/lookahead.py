"""Per-minute LOOK-AHEAD labels — vectorized over ALL minutes of the dataset.

Ben's ask names this explicitly: "strategies that involve LOOK-AHEAD for each minute, for all minutes in
our large dataset". A look-ahead label asks, AT each row's entry, a question about the FORWARD path over
the next H bars — e.g. "is this minute the start of an up-move?" (triple-barrier first touch) or "what is
the forward run-up?" (forward-window extremum). These are computed point-in-time per ENTRY row but read
FORWARD bars — they are LABELS (the thing we grade a signal against), never features.

Both are computed PER CONTIGUOUS SYMBOL BLOCK (the Panel is sorted by (symbol_code, minute), so a forward
window never crosses a symbol boundary) and VECTORIZED across symbols via a single padded (n_symbols x
max_len) array with numpy windowed ops — no Python per-row loop over the millions of dataset minutes. The
forward window is in BAR steps (the panel's native minute cadence): H=`horizon_bars` consecutive rows in
the symbol block.

Caveat (documented): the forward window is `horizon_bars` consecutive PANEL ROWS, which for the intraday
panel is `horizon_bars` SAMPLED minutes (the 30-min cadence), and for the daily panel is `horizon_bars`
trading days. A row within `horizon_bars` of its symbol-block tail has an incomplete forward window and is
nulled (NaN) — never graded on a truncated path.
"""
from __future__ import annotations

import numpy as np

from quantlib.battery.panel import Panel


def _block_bounds(symbol_code: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous [start, end) row ranges, one per symbol block (the panel is sorted by symbol_code)."""
    if symbol_code.size == 0:
        return []
    boundaries = np.flatnonzero(np.diff(symbol_code)) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [symbol_code.size]))
    return list(zip(starts.tolist(), ends.tolist()))


def up_move_start_label(panel: Panel, horizon_bars: int, barrier_bps: float) -> np.ndarray:
    """The triple-barrier first-touch LOOK-AHEAD label: at each entry row, +1 if the forward path hits
    +`barrier_bps` before -`barrier_bps` within the next `horizon_bars` bars, -1 if it hits the down
    barrier first, 0 on timeout (neither within H). NaN where the forward window is incomplete (the last
    `horizon_bars` rows of each symbol block).

    "Is this minute the start of an up-move?" The forward path uses the bar HIGH for the up barrier and the
    bar LOW for the down barrier (a touch is the bar's extreme reaching the barrier), referenced to the
    entry close. Vectorized per symbol block; within a block it is a numpy double-loop over the H forward
    offsets (H is small — 30/60 bars), NOT over the millions of rows."""
    entry = panel.entry_close
    high = panel.high
    low = panel.low
    out = np.full(panel.n_rows, np.nan)
    up_mult = 1.0 + barrier_bps / 1e4
    down_mult = 1.0 - barrier_bps / 1e4
    for start, end in _block_bounds(panel.symbol_code):
        n = end - start
        if n <= horizon_bars:
            continue
        e = entry[start:end]
        hi = high[start:end]
        lo = low[start:end]
        gradable = n - horizon_bars
        up_target = e[:gradable] * up_mult
        down_target = e[:gradable] * down_mult
        # first-touch offset for each barrier across the H forward bars (offset 1..H), per gradable row.
        first_up = np.full(gradable, horizon_bars + 1)
        first_down = np.full(gradable, horizon_bars + 1)
        rows = np.arange(gradable)
        for offset in range(1, horizon_bars + 1):
            fwd_hi = hi[rows + offset]
            fwd_lo = lo[rows + offset]
            hit_up = (fwd_hi >= up_target) & (first_up > horizon_bars)
            hit_down = (fwd_lo <= down_target) & (first_down > horizon_bars)
            first_up = np.where(hit_up, offset, first_up)
            first_down = np.where(hit_down, offset, first_down)
        label = np.where(
            (first_up > horizon_bars) & (first_down > horizon_bars),
            0.0,
            np.where(first_up < first_down, 1.0, -1.0),
        )
        out[start : start + gradable] = label
    return out


def fwd_max_runup_label(panel: Panel, horizon_bars: int) -> np.ndarray:
    """The forward-window extremum LOOK-AHEAD label: max bar HIGH over the next `horizon_bars` bars /
    entry close - 1 (the maximum forward run-up achievable from this entry). NaN where the forward window
    is incomplete. Vectorized per symbol block as a running forward max over the H offsets."""
    entry = panel.entry_close
    high = panel.high
    out = np.full(panel.n_rows, np.nan)
    for start, end in _block_bounds(panel.symbol_code):
        n = end - start
        if n <= horizon_bars:
            continue
        e = entry[start:end]
        hi = high[start:end]
        gradable = n - horizon_bars
        rows = np.arange(gradable)
        running_max = np.full(gradable, -np.inf)
        for offset in range(1, horizon_bars + 1):
            running_max = np.maximum(running_max, hi[rows + offset])
        out[start : start + gradable] = running_max / e[:gradable] - 1.0
    return out
