"""Daily-snapshot feature groups ported to the ``CleanEngine`` interface, + the shared daily-data plumbing.

A daily-snapshot group is intraday-INVARIANT: its features are computed ONCE per session from the settled
DAILY bars (prior-day closes, multi-day returns, N-day highs, ADV) and broadcast to every minute of the day.
The engine carries the daily history in the per-session memo (``window.session``), populated once at the
session boundary via ``CleanEngine.set_session`` — the daily analogue of the minute trailing buffer.

SESSION SCHEMA (the shared plumbing — built once, read by every daily-snapshot group):
  ``session["daily_close"]`` : ``(n_symbols, n_days)`` matrix, settled daily closes, newest column LAST. The
      newest column is the ``_asof`` reference (the prior completed day's close — the point-in-time anchor an
      intraday feature reads). NaN-padded on the left where a symbol has fewer days.
  ``session["daily_high"]``  : ``(n_symbols, n_days)`` settled daily highs (for N-day-high distances).
  ``session["daily_volume"]``: ``(n_symbols, n_days)`` settled daily volumes (for ADV / dollar-volume).
A group derives its features from these matrices exactly as a windowed group derives from the trailing buffer.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window


def _daily_window(mat: np.ndarray, w: int) -> np.ndarray:
    """The last ``w`` columns of a ``(n_symbols, n_days)`` daily matrix."""
    return mat[:, -w:]


def _asof(daily_close: np.ndarray) -> np.ndarray:
    """The prior completed day's close (the newest daily column) — the point-in-time daily anchor. NaN where
    the symbol has no daily history."""
    if daily_close.shape[1] == 0:
        return np.full(daily_close.shape[0], np.nan)
    return daily_close[:, -1]


def _daily_return(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Return over the last ``w`` completed days: ``_asof / close[D-1-w] − 1`` (newest col / w cols back)."""
    n_sym, n_days = daily_close.shape
    asof = _asof(daily_close)
    if n_days > w:
        ref = daily_close[:, -(w + 1)]
    else:
        ref = np.full(n_sym, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return asof / ref - 1.0


def _daily_vol(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Std (ddof=1) of the last ``w`` daily returns ending at the prior close."""
    with np.errstate(invalid="ignore", divide="ignore"):
        rets = daily_close[:, 1:] / daily_close[:, :-1] - 1.0  # (n_sym, n_days-1)
    win = _daily_window(rets, w)
    mask = np.isfinite(win)
    n = mask.sum(axis=1).astype(np.float64)
    x = np.where(mask, win, 0.0)
    sum_x = x.sum(axis=1)
    sum_x2 = (x * x).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (sum_x2 - sum_x * sum_x / n) / (n - 1.0)
        std = np.sqrt(np.clip(var, 0.0, None))
    return np.where(n > 1.0, std, np.nan)


def _dist_from_high(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Prior close relative to its trailing ``w``-day high: ``_asof / max(close over w days) − 1`` (≤ 0)."""
    asof = _asof(daily_close)
    win = _daily_window(daily_close, w)
    all_nan = ~np.isfinite(win).any(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        high = np.nanmax(np.where(np.isfinite(win), win, -np.inf), axis=1)
        dist = asof / high - 1.0
    return np.where(all_nan, np.nan, dist)


class MultiDayClean:
    """DAILY-SNAPSHOT: multi-day return / volatility / N-day-high distance from the settled daily closes,
    point-in-time as of the prior close. daily_return_{w}d, daily_vol_{w}d, dist_from_{w}d_high. Reads
    ``window.session['daily_close']``. Legacy: ``MultiDayGroup`` (DailySnapshotGroup)."""

    name = "multi_day"
    input_cols = ()  # reads only the daily snapshot, not the minute bars
    _DAY_WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60, 90, 120, 180, 240)
    _VOL_DAYS: tuple[int, ...] = (5, 10, 20, 30, 60)
    _HIGH_DAYS: tuple[int, ...] = (10, 20, 60, 120, 250)
    feature_names = (
        tuple(f"daily_return_{w}d" for w in _DAY_WINDOWS)
        + tuple(f"daily_vol_{w}d" for w in _VOL_DAYS)
        + tuple(f"dist_from_{w}d_high" for w in _HIGH_DAYS)
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        daily_close = window.session.get("daily_close")
        n = window.n
        if daily_close is None:
            return {name: np.full(n, np.nan) for name in self.feature_names}
        out: dict[str, np.ndarray] = {}
        for w in self._DAY_WINDOWS:
            out[f"daily_return_{w}d"] = _daily_return(daily_close, w)
        for w in self._VOL_DAYS:
            out[f"daily_vol_{w}d"] = _daily_vol(daily_close, w)
        for w in self._HIGH_DAYS:
            out[f"dist_from_{w}d_high"] = _dist_from_high(daily_close, w)
        return out


def _daily_return_matrix(daily_close: np.ndarray) -> np.ndarray:
    """Per-symbol daily-return matrix from the daily-close matrix: ``close[d]/close[d-1] − 1``, (n_sym,
    n_days-1)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return daily_close[:, 1:] / daily_close[:, :-1] - 1.0


_DAILY_BETA_WINDOW = 60
_DAILY_BETA_MIN_PAIRS = 20


class DailyBetaClean:
    """DAILY-SNAPSHOT: rolling 60-day OLS beta/corr/idio-vol of the name's DAILY returns on SPY's daily returns
    (the certified W11 overnight-beta quantity). daily_beta_60d = cov(name,mkt)/var(mkt); daily_corr_60d (clip
    [-1,1]); daily_idio_vol_60d = name_std·sqrt(1−corr²). NaN if <20 finite pairs or SPY var=0. Reads
    ``window.session['daily_close']`` + the SPY row index from ``window.static['spy_row']``. Legacy:
    ``DailyBetaGroup``."""

    name = "daily_beta"
    input_cols = ()
    feature_names = ("daily_beta_60d", "daily_corr_60d", "daily_idio_vol_60d")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        nan = np.full(n, np.nan)
        daily_close = window.session.get("daily_close")
        spy_row = window.static.get("spy_row")
        if daily_close is None or spy_row is None:
            return {name: nan for name in self.feature_names}
        spy_idx = int(np.asarray(spy_row).flat[0])
        rets = _daily_return_matrix(daily_close)  # (n_sym, n_days-1)
        mkt = rets[spy_idx]  # SPY's daily returns (1-D)
        # trailing 60-day window of paired (name, mkt) daily returns; both finite.
        name_w = rets[:, -_DAILY_BETA_WINDOW:]
        mkt_w = np.broadcast_to(mkt[-_DAILY_BETA_WINDOW:], name_w.shape)
        mask = np.isfinite(name_w) & np.isfinite(mkt_w)
        npairs = mask.sum(axis=1).astype(np.float64)
        x = np.where(mask, mkt_w, 0.0)  # market (regressor)
        y = np.where(mask, name_w, 0.0)  # name (regressand)
        sx, sy = x.sum(axis=1), y.sum(axis=1)
        sxx, syy, sxy = (x * x).sum(axis=1), (y * y).sum(axis=1), (x * y).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            cov = sxy / npairs - (sx / npairs) * (sy / npairs)
            var_x = sxx / npairs - (sx / npairs) ** 2
            var_y = syy / npairs - (sy / npairs) ** 2
            beta = cov / var_x
            corr = cov / np.sqrt(var_x * var_y)
            idio = np.sqrt(np.clip(var_y * npairs / (npairs - 1.0), 0.0, None)) * np.sqrt(
                np.clip(1.0 - corr * corr, 0.0, None)
            )
        defined = (npairs >= _DAILY_BETA_MIN_PAIRS) & (var_x > 0.0)
        defined_corr = defined & (var_y > 0.0)
        return {
            "daily_beta_60d": np.where(defined, beta, np.nan),
            "daily_corr_60d": np.where(defined_corr, np.clip(corr, -1.0, 1.0), np.nan),
            "daily_idio_vol_60d": np.where(defined_corr, idio, np.nan),
        }
