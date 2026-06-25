"""Windowed-kind feature groups ported to the ``CleanEngine`` interface (the ReductionGroup batch).

Each group's legacy ``reduced()/assemble()`` declarative polars form becomes ONE numpy ``compute(window)`` over
the carried trailing buffer: read ``window.trailing(col)`` (the per-symbol ``(n_symbols, buffer)`` matrix), take
the last ``w`` columns per feature window, reduce over axis 1. Same arithmetic, framework-free, one path (live ==
backfill replay). Guards and NaN policy match each group's declared ``FeatureSpec`` contract.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window


def _trailing_window(mat: np.ndarray, w: int) -> np.ndarray:
    """The last ``w`` columns of the ``(n_symbols, buffer)`` trailing matrix — each symbol's most recent ``w``
    present bars (NaN-padded on the left where it has fewer)."""
    return mat[:, -w:]


def _masked_mean(values: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Trailing ``w``-window mean of ``values`` ignoring NaN, plus the per-symbol present-count. Returns
    ``(mean, n_present)``; ``mean`` is NaN where ``n_present == 0`` (warm-up / all-NaN window)."""
    win = _trailing_window(values, w)
    mask = np.isfinite(win)
    n_present = mask.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(mask, win, 0.0).sum(axis=1) / n_present
    return np.where(n_present > 0, mean, np.nan), n_present


def _masked_std(values: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window SAMPLE std (ddof=1, matching the legacy ``std_`` reduction) of ``values`` ignoring
    NaN. NaN where fewer than 2 present bars (the count>1 guard). std = sqrt((Σx² − (Σx)²/n)/(n−1))."""
    win = _trailing_window(values, w)
    mask = np.isfinite(win)
    n = mask.sum(axis=1).astype(np.float64)
    x = np.where(mask, win, 0.0)
    sum_x = x.sum(axis=1)
    sum_x2 = (x * x).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (sum_x2 - sum_x * sum_x / n) / (n - 1.0)
        std = np.sqrt(np.clip(var, 0.0, None))
    return np.where(n > 1.0, std, np.nan)


def _returns(close: np.ndarray) -> np.ndarray:
    """Per-bar close-to-close return matrix ``close[t]/close[t-1] - 1`` over the trailing buffer; the first
    column (no prior bar) is NaN. Matches ``close/close.shift(1) - 1``."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close[:, 1:] / close[:, :-1] - 1.0
    return np.concatenate([np.full((close.shape[0], 1), np.nan), ret], axis=1)


_FOUR_LN2 = 2.772588722239781


class VolatilityClean:
    """VOLATILITY: point-in-time high_low_range_1m = (high-low)/close; realized_vol_{w}m = sample std (ddof=1)
    of one-minute close-to-close returns; parkinson_vol_{w}m = sqrt(clip(mean(ln(H/L)²)/(4ln2), ≥0)). Legacy:
    ``VolatilityGroup`` (ReductionGroup, volatility.py)."""

    name = "volatility"
    input_cols = ("high", "low", "close")
    _VOL_WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
    _RANGE_WINDOWS: tuple[int, ...] = (15, 30, 60, 120)
    feature_names = (
        ("high_low_range_1m",)
        + tuple(f"realized_vol_{w}m" for w in _VOL_WINDOWS)
        + tuple(f"parkinson_vol_{w}m" for w in _RANGE_WINDOWS)
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        high = window.trailing("high")
        low = window.trailing("low")
        close = window.trailing("close")
        latest_high = window.latest("high")
        latest_low = window.latest("low")
        latest_close = window.latest("close")
        ret = _returns(close)
        with np.errstate(invalid="ignore", divide="ignore"):
            hl2 = np.log(high / low) ** 2
        out: dict[str, np.ndarray] = {
            "high_low_range_1m": (latest_high - latest_low) / latest_close,
        }
        for w in self._VOL_WINDOWS:
            out[f"realized_vol_{w}m"] = _masked_std(ret, w)
        for w in self._RANGE_WINDOWS:
            mean_hl2, _ = _masked_mean(hl2, w)
            with np.errstate(invalid="ignore"):
                out[f"parkinson_vol_{w}m"] = np.sqrt(np.clip(mean_hl2 / _FOUR_LN2, 0.0, None))
        return out


class QuoteSpreadClean:
    """QUOTE_SPREAD (Layer B): point-in-time last-minute spread/imbalance/depth + trailing means of spread and
    imbalance over each window. ``spread_bps_1m``/``quote_imbalance_1m`` = latest; ``book_depth_1m`` =
    latest(bid_size+ask_size); ``spread_bps_{w}m``/``quote_imbalance_{w}m`` = trailing means. nan_policy=sparse
    (absent quote minute -> NaN, which the masked mean / latest already yield). Legacy: ``QuoteSpreadGroup``."""

    name = "quote_spread"
    input_cols = ("mean_spread_bps", "quote_imbalance", "mean_bid_size", "mean_ask_size")
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)
    feature_names = ("spread_bps_1m", "quote_imbalance_1m", "book_depth_1m") + tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("spread_bps", "quote_imbalance")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        spread = window.trailing("mean_spread_bps")
        imbalance = window.trailing("quote_imbalance")
        bid_size = window.latest("mean_bid_size")
        ask_size = window.latest("mean_ask_size")
        out: dict[str, np.ndarray] = {
            "spread_bps_1m": window.latest("mean_spread_bps"),
            "quote_imbalance_1m": window.latest("quote_imbalance"),
            "book_depth_1m": bid_size + ask_size,
        }
        for w in self._WINDOWS:
            out[f"spread_bps_{w}m"], _ = _masked_mean(spread, w)
            out[f"quote_imbalance_{w}m"], _ = _masked_mean(imbalance, w)
        return out


_LN2 = 0.6931471805599453


class OhlcVolClean:
    """VOLATILITY: OHLC-efficient per-bar variance estimators averaged over the window then square-rooted.
    Garman-Klass = 0.5·ln(H/L)² − (2ln2−1)·ln(C/O)²; Rogers-Satchell = ln(H/C)ln(H/O) + ln(L/C)ln(L/O).
    Both clipped to ≥0 before the root. Legacy: ``OhlcVolGroup`` (ReductionGroup, ohlc_vol.py)."""

    name = "ohlc_vol"
    input_cols = ("open", "high", "low", "close")
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("garman_klass_vol", "rogers_satchell_vol")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        op = window.trailing("open")
        high = window.trailing("high")
        low = window.trailing("low")
        close = window.trailing("close")
        with np.errstate(invalid="ignore", divide="ignore"):
            ln_hl = np.log(high / low)
            ln_co = np.log(close / op)
            ln_hc = np.log(high / close)
            ln_ho = np.log(high / op)
            ln_lc = np.log(low / close)
            ln_lo = np.log(low / op)
        gk_var = 0.5 * ln_hl * ln_hl - (2.0 * _LN2 - 1.0) * ln_co * ln_co
        rs_var = ln_hc * ln_ho + ln_lc * ln_lo
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            gk_mean, _ = _masked_mean(gk_var, w)
            rs_mean, _ = _masked_mean(rs_var, w)
            with np.errstate(invalid="ignore"):
                out[f"garman_klass_vol_{w}m"] = np.sqrt(np.clip(gk_mean, 0.0, None))
                out[f"rogers_satchell_vol_{w}m"] = np.sqrt(np.clip(rs_mean, 0.0, None))
        return out


class RangeExpansionClean:
    """VOLATILITY: ratio of a recent-window mean intrabar range to a trailing-window mean —
    ``range_expansion_{recent}_{trailing}m`` = mean((high-low)/close over ``recent``) / same over ``trailing``.
    > 1 = range expanding (vol-burst precursor), < 1 contracting. A ratio of two windowed means of the same
    non-negative per-bar ratio. Legacy: ``RangeExpansionGroup`` (ReductionGroup, range_expansion.py)."""

    name = "range_expansion"
    input_cols = ("high", "low", "close")
    _WINDOW_PAIRS: tuple[tuple[int, int], ...] = ((5, 30), (10, 60))
    feature_names = tuple(f"range_expansion_{recent}_{trailing}m" for recent, trailing in _WINDOW_PAIRS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        high = window.trailing("high")
        low = window.trailing("low")
        close = window.trailing("close")
        # per-bar realized range fraction; Guard 2: close>0, else NaN (excluded from both window means).
        with np.errstate(invalid="ignore", divide="ignore"):
            rng = np.where(close > 0.0, (high - low) / close, np.nan)
        out: dict[str, np.ndarray] = {}
        for recent, trailing in self._WINDOW_PAIRS:
            num, _ = _masked_mean(rng, recent)
            denom, _ = _masked_mean(rng, trailing)
            # Guard 2: denom is a mean of non-negative terms — sign-robust, denom>0 is sufficient. A
            # flat/zero-range trailing window -> NaN. is_finite backstop folds any stray non-finite to NaN.
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(denom > 0.0, num / denom, np.nan)
            out[f"range_expansion_{recent}_{trailing}m"] = np.where(np.isfinite(ratio), ratio, np.nan)
        return out
