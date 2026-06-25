"""Cross-sectional feature groups ported to the ``CleanEngine`` interface.

These reduce over the SYMBOL axis at a single minute (a name's rank / dispersion / beta versus the whole
universe present that minute), the opposite axis from the windowed groups. The engine's ``compute(window)``
already sees every symbol's trailing matrices, so the cross-section is a numpy reduce over axis 0 — but it MUST
gate on ``window.present()``: a symbol that delivered no bar this minute carries its last value, so including it
in a rank / count / dispersion would let a stale name shift everyone's cross-sectional value (the breadth bug
class). Only this-minute-present symbols participate.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window


def _average_rank(values: np.ndarray) -> np.ndarray:
    """Average-method ranks (ties share the mean of their rank span), 1-based, over the finite entries of a
    1-D array — matching polars ``rank(method="average")``. NaN entries get NaN rank."""
    finite = np.isfinite(values)
    out = np.full(values.shape, np.nan)
    if not finite.any():
        return out
    idx = np.where(finite)[0]
    vals = values[idx]
    order = np.argsort(vals, kind="stable")
    sorted_vals = vals[order]
    # ordinal ranks 1..m for the sorted positions, then average over tie-groups
    m = len(vals)
    ordinal = np.arange(1, m + 1, dtype=np.float64)
    avg = ordinal.copy()
    start = 0
    for end in range(1, m + 1):
        if end == m or sorted_vals[end] != sorted_vals[start]:
            avg[start:end] = ordinal[start:end].mean()
            start = end
    ranks = np.empty(m, dtype=np.float64)
    ranks[order] = avg
    out[idx] = ranks
    return out


def _cross_sectional_percentile(values: np.ndarray, present: np.ndarray) -> np.ndarray:
    """Percentile rank in [0, 1] of each PRESENT symbol's ``value`` across the present cross-section:
    ``(rank − 1)/(n − 1)`` over present+finite entries, NaN where n<2 or the symbol is absent/non-finite.
    Mirrors the legacy ``_percentile_over_minute`` (rank scaled to [0,1], null when <2 names present)."""
    masked = np.where(present, values, np.nan)
    ranks = _average_rank(masked)
    n = np.isfinite(masked).sum()
    if n < 2:
        return np.full(values.shape, np.nan)
    return (ranks - 1.0) / (n - 1.0)


class CrossSectionalRankClean:
    """CROSS_SECTIONAL: percentile rank (0-1) of each present symbol's trailing return / volume / dollar-volume
    across the present cross-section that minute. return_rank over (5,15,30,60), volume_rank_1m,
    dollar_volume_rank_1m. Absent symbols excluded from the rank set (present()-gated). Legacy:
    ``CrossSectionalRankGroup`` (FeatureGroup, cross_sectional_rank.py)."""

    name = "cross_sectional_rank"
    input_cols = ("close", "volume")
    _RETURN_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
    feature_names = tuple(f"return_rank_{w}m" for w in _RETURN_WINDOWS) + (
        "volume_rank_1m",
        "dollar_volume_rank_1m",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        present = window.present()
        latest_close = window.latest("close")
        latest_volume = window.latest("volume")
        out: dict[str, np.ndarray] = {}
        for w in self._RETURN_WINDOWS:
            # trailing w-minute return: latest close / close w bars back − 1 (per symbol)
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(close.shape[0], np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = latest_close / prior - 1.0
            out[f"return_rank_{w}m"] = _cross_sectional_percentile(ret, present)
        out["volume_rank_1m"] = _cross_sectional_percentile(latest_volume, present)
        with np.errstate(invalid="ignore"):
            dollar = latest_close * latest_volume
        out["dollar_volume_rank_1m"] = _cross_sectional_percentile(dollar, present)
        return out
