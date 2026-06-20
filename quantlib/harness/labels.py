"""Forward-return labels derived off the panel's resident arrays (no store re-read, gap-safe).

The label is the per-row forward CROSS-SECTIONAL EXCESS return at the configured horizon — "did this
name out/under-perform its peers over the next k periods?" — which is exactly what a dollar-neutral L/S
basket harvests. Excess (vs the per-timestamp cross-sectional mean) so a market move doesn't masquerade
as signal.

DAILY cadence: forward k-trading-day close-to-close return per symbol (`rth_close[t+k]/entry[t]-1`),
computed with a per-symbol forward shift over the contiguous symbol blocks (gap-safe: the panel is
sorted by (symbol_code, minute), so a shift within a symbol block never crosses symbols), then made
cross-sectional-excess per timestamp. The $1 floor was already enforced in the panel build.

INTRADAY cadence: the panel already carries `fwd_<h>m` (cross-sectional excess) in `extra` from
`build_intraday_panel`; we read the requested horizon directly.
"""
from __future__ import annotations

import numpy as np

from quantlib.battery.panel import Panel


def forward_excess_label(panel: Panel, *, horizon_days: int, horizon_min: int) -> np.ndarray:
    """Per-row forward cross-sectional excess return at the configured horizon. NaN where the forward
    price is unavailable (the tail of each symbol block) or the timestamp has too few names."""
    if panel.cadence == "daily":
        raw = _daily_forward_return(panel, horizon_days)
    else:
        raw = _intraday_forward_return(panel, horizon_min)
    return _cross_sectional_excess(raw, panel.minute_epoch)


def _daily_forward_return(panel: Panel, horizon_days: int) -> np.ndarray:
    """`rth_close[t + horizon_days] / entry_close[t] - 1`, shifted WITHIN each contiguous symbol block
    (the panel is sorted by symbol_code then minute, so a forward shift never crosses a symbol). NaN at
    the last `horizon_days` rows of each symbol block (no forward price)."""
    close = panel.extra["rth_close"]
    entry = panel.entry_close
    symbol_code = panel.symbol_code
    n = close.shape[0]
    forward_close = np.full(n, np.nan)
    # shift close back by horizon within each symbol block; the block boundary is where symbol_code
    # changes. A forward index that lands in a different symbol block is invalid -> NaN.
    if n > horizon_days:
        same_symbol = symbol_code[horizon_days:] == symbol_code[:-horizon_days]
        shifted = np.where(same_symbol, close[horizon_days:], np.nan)
        forward_close[:-horizon_days] = shifted
    return forward_close / entry - 1.0


def _intraday_forward_return(panel: Panel, horizon_min: int) -> np.ndarray:
    """Read the pre-computed forward-excess column `fwd_<h>m` carried in the intraday panel's `extra`.
    (It is already cross-sectional excess; `_cross_sectional_excess` is idempotent-safe on it because a
    second de-mean of an already-de-meaned series leaves it unchanged.)"""
    key = f"fwd_{horizon_min}m"
    if key not in panel.extra:
        available = [k for k in panel.extra if k.startswith("fwd_") and k.endswith("m")]
        raise KeyError(f"intraday label {key} not in panel; available forward labels: {available}")
    return panel.extra[key]


def _cross_sectional_excess(raw: np.ndarray, minute_epoch: np.ndarray, *, min_names: int = 20) -> np.ndarray:
    """Subtract the per-timestamp cross-sectional MEAN (breadth-floored). A timestamp with < min_names
    finite values is nulled (too thin a cross-section to de-mean reliably)."""
    out = np.full(raw.shape[0], np.nan)
    order = np.argsort(minute_epoch, kind="stable")
    sorted_epoch = minute_epoch[order]
    sorted_raw = raw[order]
    boundaries = np.flatnonzero(np.diff(sorted_epoch)) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [raw.shape[0]]))
    for start, end in zip(starts, ends):
        block = sorted_raw[start:end]
        finite = np.isfinite(block)
        if int(finite.sum()) < min_names:
            continue
        mean = float(np.mean(block[finite]))
        excess = np.where(finite, block - mean, np.nan)
        out[order[start:end]] = excess
    return out
