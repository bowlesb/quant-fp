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


# Legacy OLS denominator floors (quant-fp declarative.py:104-146): the X-side conditioning that gates a
# well-posed regression. A plain ``denom_x > 0`` is NOT enough — on a near-constant (but non-zero) x window,
# denom_x is a tiny float-noise residue whose ratio is a SPURIOUS slope/corr; legacy NaNs it. BOTH floors must
# hold (relative to (Σx)² AND to the scale-invariant b·Σx² CoV²). This is the #402/#122/#131 corr-denom footgun
# — match it EXACTLY or the clean engine emits a spurious correlation where backfill emits NaN on flat/illiquid
# names (a fingerprint-corrupting live==backfill divergence).
_OLS_DENOM_X_REL_EPS = 1e-12
_OLS_DENOM_X_CENTERED_REL_EPS = 1e-9
_OLS_DENOM_Y_REL_EPS = 1e-12  # corr y-side floor (non-anchored)
_OLS_PERFECT_FIT_COUNT = 2.0  # a line through exactly 2 points: r2==1, corr==sign(cov)


def _ols_sums(x_mat: np.ndarray, y_mat: np.ndarray, w: int) -> tuple[np.ndarray, ...]:
    """Masked power sums for a trailing ``w``-window OLS of ``y`` on ``x`` (bars where BOTH finite). Returns
    ``(n, sx, sy, sxx, syy, sxy, denom_x, cov)`` — the shared front-half of slope/corr."""
    xw = _trailing_window(x_mat, w)
    yw = _trailing_window(y_mat, w)
    mask = np.isfinite(xw) & np.isfinite(yw)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, xw, 0.0)
    yf = np.where(mask, yw, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        denom_x = n * sxx - sx * sx
        cov = n * sxy - sx * sy
    return n, sx, sy, sxx, syy, sxy, denom_x, cov


def _denom_x_defined(n: np.ndarray, sx: np.ndarray, sxx: np.ndarray, denom_x: np.ndarray) -> np.ndarray:
    """The legacy X-side well-posedness gate: n≥2 AND both relative denom_x floors hold (vs (Σx)² and n·Σx²)."""
    return (
        (n >= 2.0)
        & (denom_x > _OLS_DENOM_X_REL_EPS * (sx * sx))
        & (denom_x > _OLS_DENOM_X_CENTERED_REL_EPS * (n * sxx))
    )


def _windowed_ols_slope(x_mat: np.ndarray, y_mat: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window OLS slope of ``y`` on ``x`` per symbol — matching the legacy ``slope_`` reduction:
    slope = cov / denom_x, defined ONLY when the legacy X-side floors hold (n≥2 AND denom_x > 1e-12·(Σx)² AND
    denom_x > 1e-9·n·Σx²). NaN otherwise — a near-constant x window is not a well-posed regression."""
    n, sx, _sy, sxx, _syy, _sxy, denom_x, cov = _ols_sums(x_mat, y_mat, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = cov / denom_x
    return np.where(_denom_x_defined(n, sx, sxx, denom_x), slope, np.nan)


def _windowed_cov(x_mat: np.ndarray, y_mat: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window sample covariance of ``x`` and ``y`` per symbol over bars where BOTH finite,
    population form ``Σ(xy)/n − (Σx/n)(Σy/n)`` (matching the legacy Roll-spread autocovariance assembly).
    NaN where n<2."""
    xw = _trailing_window(x_mat, w)
    yw = _trailing_window(y_mat, w)
    mask = np.isfinite(xw) & np.isfinite(yw)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, xw, 0.0)
    yf = np.where(mask, yw, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = (xf * yf).sum(axis=1) / n - (xf.sum(axis=1) / n) * (yf.sum(axis=1) / n)
    return np.where(n >= 2.0, cov, np.nan)


def _windowed_corr(x_mat: np.ndarray, y_mat: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window Pearson correlation of ``x`` and ``y`` per symbol — matching the legacy ``corr_``
    reduction EXACTLY: r = cov / sqrt(denom_x·denom_y), defined ONLY when the X-side floors hold AND
    denom_y > 1e-12·(Σy)². A line through exactly 2 present points (perfect fit) returns sign(cov), not the
    ratio. A near-constant x OR y window is NaN — never a spurious correlation of float noise (the corr-denom
    footgun that gates ``incremental_safe``)."""
    n, sx, sy, sxx, syy, _sxy, denom_x, cov = _ols_sums(x_mat, y_mat, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        denom_y = n * syy - sy * sy
        defined_corr = _denom_x_defined(n, sx, sxx, denom_x) & (denom_y > _OLS_DENOM_Y_REL_EPS * (sy * sy))
        corr = cov / np.sqrt(denom_x * denom_y)
    corr = np.where(defined_corr, corr, np.nan)
    # perfect fit (exactly 2 present points): |corr| == 1, the sign of the covariance.
    perfect = defined_corr & (n == _OLS_PERFECT_FIT_COUNT)
    return np.where(perfect, np.sign(cov), corr)


def _windowed_sum(values: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window sum of ``values`` ignoring NaN (NaN bars contribute 0; an all-NaN window sums to
    0) — matching the legacy ``sum_`` reduction over present bars."""
    win = _trailing_window(values, w)
    return np.where(np.isfinite(win), win, 0.0).sum(axis=1)


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


def _windowed_max(values: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window max ignoring NaN; NaN where the window has no present bar."""
    win = _trailing_window(values, w)
    all_nan = ~np.isfinite(win).any(axis=1)
    with np.errstate(invalid="ignore"):
        out = np.nanmax(np.where(np.isfinite(win), win, -np.inf), axis=1)
    return np.where(all_nan, np.nan, out)


def _windowed_min(values: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window min ignoring NaN; NaN where the window has no present bar."""
    win = _trailing_window(values, w)
    all_nan = ~np.isfinite(win).any(axis=1)
    with np.errstate(invalid="ignore"):
        out = np.nanmin(np.where(np.isfinite(win), win, np.inf), axis=1)
    return np.where(all_nan, np.nan, out)


def _row_nanmax(mat: np.ndarray) -> np.ndarray:
    """Per-row max over a full ``(n, k)`` matrix ignoring NaN; NaN where a row is all-NaN. For an already
    TIME-windowed matrix (from ``trailing_time``) — every finite cell is in the window."""
    all_nan = ~np.isfinite(mat).any(axis=1)
    with np.errstate(invalid="ignore"):
        out = np.nanmax(np.where(np.isfinite(mat), mat, -np.inf), axis=1)
    return np.where(all_nan, np.nan, out)


def _row_nanmin(mat: np.ndarray) -> np.ndarray:
    """Per-row min over a full ``(n, k)`` matrix ignoring NaN; NaN where a row is all-NaN."""
    all_nan = ~np.isfinite(mat).any(axis=1)
    with np.errstate(invalid="ignore"):
        out = np.nanmin(np.where(np.isfinite(mat), mat, np.inf), axis=1)
    return np.where(all_nan, np.nan, out)


def _row_sum(mat: np.ndarray) -> np.ndarray:
    """Per-row sum over a full ``(n, k)`` matrix, NaN bars → 0 (an all-NaN row sums to 0). For an already
    TIME-windowed matrix (from ``trailing_time``)."""
    return np.where(np.isfinite(mat), mat, 0.0).sum(axis=1)


def _row_mean(mat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-row mean of a full ``(n, k)`` matrix ignoring NaN, + the present-count. NaN where the row is empty.
    For an already TIME-windowed matrix."""
    mask = np.isfinite(mat)
    n = mask.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(mask, mat, 0.0).sum(axis=1) / n
    return np.where(n > 0, mean, np.nan), n


def _row_std(mat: np.ndarray) -> np.ndarray:
    """Per-row SAMPLE std (ddof=1) of a full ``(n, k)`` matrix ignoring NaN; NaN where fewer than 2 present. For
    an already TIME-windowed matrix."""
    mask = np.isfinite(mat)
    n = mask.sum(axis=1).astype(np.float64)
    x = np.where(mask, mat, 0.0)
    sum_x = x.sum(axis=1)
    sum_x2 = (x * x).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (sum_x2 - sum_x * sum_x / n) / (n - 1.0)
        std = np.sqrt(np.clip(var, 0.0, None))
    return np.where(n > 1.0, std, np.nan)


def _rebased_minute_axis(minute: np.ndarray) -> np.ndarray:
    """The ``(n_symbols, window)`` ``kind="time"`` OLS x-axis = the actual bar MINUTE rebased to the earliest
    present minute, in minutes: ``(minute − origin)/60``; empty slots (-1) → NaN. The rebase is load-bearing —
    raw epoch-minutes (~1e7) blow up ``(Σx)²`` (~1e15) → catastrophic cancellation in ``denom_x`` → a spurious
    OLS-floor reject. A frame-relative axis is also what the legacy ``kind="time"`` regression uses (the slope is
    translation-invariant, so the rebase is value-neutral on the slope itself)."""
    minute = minute.astype(np.float64)
    present_min = minute[minute >= 0]
    origin = present_min.min() if present_min.size else 0.0
    return np.where(minute >= 0, (minute - origin) / 60.0, np.nan)


def _row_cov(x_mat: np.ndarray, y_mat: np.ndarray) -> np.ndarray:
    """Sample covariance ``Σxy/n − (Σx/n)(Σy/n)`` over a full ``(n, k)`` matrix where BOTH finite — the
    TIME-windowed twin of ``_windowed_cov`` (caller pre-masks to the (T−w, T] window). NaN where n<2."""
    mask = np.isfinite(x_mat) & np.isfinite(y_mat)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, x_mat, 0.0)
    yf = np.where(mask, y_mat, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = (xf * yf).sum(axis=1) / n - (xf.sum(axis=1) / n) * (yf.sum(axis=1) / n)
    return np.where(n >= 2.0, cov, np.nan)


def _row_ols_slope(x_mat: np.ndarray, y_mat: np.ndarray) -> np.ndarray:
    """OLS slope of ``y`` on ``x`` over a full ``(n, k)`` matrix where BOTH finite — the TIME-windowed twin of
    ``_windowed_ols_slope`` (the caller has already masked the matrix to the (T−w, T] minutes; every finite cell
    is in-window). Same legacy X-side floors (n≥2 AND denom_x > 1e-12·(Σx)² AND > 1e-9·n·Σx²)."""
    mask = np.isfinite(x_mat) & np.isfinite(y_mat)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, x_mat, 0.0)
    yf = np.where(mask, y_mat, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, sxy = (xf * xf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        denom_x = n * sxx - sx * sx
        cov = n * sxy - sx * sy
        slope = cov / denom_x
    return np.where(_denom_x_defined(n, sx, sxx, denom_x), slope, np.nan)


def _row_corr(x_mat: np.ndarray, y_mat: np.ndarray) -> np.ndarray:
    """Pearson correlation of ``x`` and ``y`` over a full ``(n, k)`` matrix where BOTH finite — the TIME-windowed
    twin of ``_windowed_corr`` (caller pre-masks to the (T−w, T] window). Same legacy denom floors + perfect-fit
    (n==2 → sign(cov)) — a near-constant x OR y window is NaN, never a spurious correlation of float noise.
    """
    mask = np.isfinite(x_mat) & np.isfinite(y_mat)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, x_mat, 0.0)
    yf = np.where(mask, y_mat, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        denom_x = n * sxx - sx * sx
        denom_y = n * syy - sy * sy
        cov = n * sxy - sx * sy
        defined = _denom_x_defined(n, sx, sxx, denom_x) & (denom_y > _OLS_DENOM_Y_REL_EPS * (sy * sy))
        corr = cov / np.sqrt(denom_x * denom_y)
    corr = np.where(defined, corr, np.nan)
    perfect = defined & (n == _OLS_PERFECT_FIT_COUNT)
    return np.where(perfect, np.sign(cov), corr)


_RESID_MIN_POINTS = 4.0  # legacy resid_std minimum paired count for a meaningful residual distribution
_RESID_REL_FLOOR = 1e-6  # legacy resid_std relative-spread cutoff (near-perfect-fit → undefined)


def _time_ols_slope_r2_resid(
    x_mat: np.ndarray, y_mat: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The time-OLS bundle (slope, r2, resid_std%, mean_y, n) of y on x over a pre-masked (n,k) TIME window,
    matching the legacy ``_ols_stat_exprs`` centered-sum algebra EXACTLY (so the difference-of-sums rounds
    identically). x = the rebased-minute axis (the caller masks both to the (T−w,T] window). slope uses the
    legacy X-side denom floors; r2 the corr-denom floors + perfect-fit n==2→1.0; resid_std = √(SSR/n)/ȳ·100
    with SSR = syy_c − slope·sxy_c (clip≥0), guarded by n≥4 & sxx_c>0 & resid_var > (1e-6·ȳ)²."""
    mask = np.isfinite(x_mat) & np.isfinite(y_mat)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, x_mat, 0.0)
    yf = np.where(mask, y_mat, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        denom_x = n * sxx - sx * sx
        denom_y = n * syy - sy * sy
        cov = n * sxy - sx * sy
        slope = cov / denom_x
        x_ok = _denom_x_defined(n, sx, sxx, denom_x)
        slope = np.where(x_ok, slope, np.nan)
        # r2 (corr² with the legacy floors + perfect-fit n==2 → 1.0)
        defined_corr = x_ok & (denom_y > _OLS_DENOM_Y_REL_EPS * (sy * sy))
        r2 = np.where(defined_corr, (cov * cov) / (denom_x * denom_y), np.nan)
        r2 = np.where(defined_corr & (n == _OLS_PERFECT_FIT_COUNT), 1.0, r2)
        # resid_std% via the centered-sum SSR (the same algebra the legacy resid_std stat uses)
        sxx_c = sxx - sx * sx / n
        sxy_c = sxy - sx * sy / n
        syy_c = syy - sy * sy / n
        slope_r = sxy_c / sxx_c
        ssr = np.clip(syy_c - slope_r * sxy_c, 0.0, None)
        mean_y = sy / n
        resid_var = ssr / n
        resid_floor = (_RESID_REL_FLOOR * mean_y) ** 2
        resid_defined = (n >= _RESID_MIN_POINTS) & (sxx_c > 0.0) & (resid_var > resid_floor)
        resid_std = np.where(resid_defined, np.sqrt(resid_var) / mean_y * 100.0, np.nan)
    return slope, r2, resid_std, mean_y, n


def _time_ols_resid_skew(x_mat: np.ndarray, y_mat: np.ndarray) -> np.ndarray:
    """Window-LOCAL standardized 3rd residual moment (m3/m2^1.5) of a y-on-x OLS over a pre-masked (n,k) TIME
    window — legacy ``momentum_run._residual_skew_from_lists``. x = window-relative minutes (centered on the
    window mean); residuals r = y_c − slope·x_c with slope = sxy_c/sxx_c; m2 = mean(r²), m3 = mean(r³). NULL
    where n < 4, sxx_c == 0, or m2 ≤ (1e-6·mean_y)² (the near-linear residual-floor that stops m3/m2^1.5 blowing
    up on float-noise variance)."""
    mask = np.isfinite(x_mat) & np.isfinite(y_mat)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, x_mat, 0.0)
    yf = np.where(mask, y_mat, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_x = xf.sum(axis=1) / n
        mean_y = yf.sum(axis=1) / n
        xc = np.where(mask, x_mat - mean_x[:, None], 0.0)
        yc = np.where(mask, y_mat - mean_y[:, None], 0.0)
        sxx_c = (xc * xc).sum(axis=1)
        sxy_c = (xc * yc).sum(axis=1)
        slope = sxy_c / sxx_c
        resid = np.where(mask, yc - slope[:, None] * xc, 0.0)
        m2 = (resid * resid).sum(axis=1) / n
        m3 = (resid * resid * resid).sum(axis=1) / n
        resid_floor = (_RESID_REL_FLOOR * mean_y) ** 2
        defined = (n >= _RESID_MIN_POINTS) & (sxx_c > 0.0) & (m2 > resid_floor)
        skew = m3 / np.clip(m2, 0.0, None) ** 1.5
    return np.where(defined, skew, np.nan)


def _global_run_length(ret: np.ndarray) -> np.ndarray:
    """Per-bar GLOBAL run length of consecutive same-direction nonzero returns over the trailing return matrix
    (NaN where ret absent). At each present bar: how many bars the same-sign run has reached (0 on a zero
    return — a flat bar breaks the run). The legacy ``_global_run_length`` over present-return rows; here over
    the per-symbol buffer (absent bars don't break a run — they're simply not present-return rows)."""
    n_sym, k = ret.shape
    sign = np.where(ret > 0.0, 1, np.where(ret < 0.0, -1, 0))
    present = np.isfinite(ret)
    run = np.zeros((n_sym, k), dtype=np.int64)
    prev_sign = np.zeros(n_sym, dtype=np.int64)
    prev_run = np.zeros(n_sym, dtype=np.int64)
    for t in range(k):
        col_present = present[:, t]
        col_sign = sign[:, t]
        # continue the run iff this present bar's sign equals the prior present bar's sign and is nonzero.
        same = col_present & (col_sign != 0) & (col_sign == prev_sign)
        new_run = np.where(same, prev_run + 1, np.where(col_present & (col_sign != 0), 1, 0))
        run[:, t] = np.where(col_present, new_run, 0)
        # carry the run state forward only across PRESENT bars (an absent bar leaves prev_* unchanged).
        prev_sign = np.where(col_present, col_sign, prev_sign)
        prev_run = np.where(col_present, new_run, prev_run)
    return run


_RANGE_REL_EPS = 1e-9


class PriceLevelsClean:
    """PRICE: where close sits in its trailing high-low range. position_in_range_{w}m = (close − min_low)/
    (max_high − min_low), NULL on a flat window (band ≤ 1e-9·|high|); dist_from_high_{w}m = close/max_high − 1
    (≤0); dist_from_low_{w}m = close/min_low − 1 (≥0). Trailing max/min over w bars. Legacy: ``PriceLevelGroup``
    (StatefulGroup base, but the math is a windowed max/min read)."""

    name = "price_levels"
    input_cols = ("close", "high", "low")
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60, 120, 240)
    feature_names = tuple(
        f"{prefix}_{w}m"
        for w in _WINDOWS
        for prefix in ("position_in_range", "dist_from_high", "dist_from_low")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy rolling_max_by/rolling_min_by over minute): the trailing-w-MINUTE matrix.
            high_mat = window.trailing_time("high", w)
            low_mat = window.trailing_time("low", w)
            high_w = _row_nanmax(high_mat)
            low_w = _row_nanmin(low_mat)
            band = high_w - low_w
            with np.errstate(invalid="ignore", divide="ignore"):
                pos = np.where(band > _RANGE_REL_EPS * np.abs(high_w), (close - low_w) / band, np.nan)
                out[f"position_in_range_{w}m"] = pos
                out[f"dist_from_high_{w}m"] = close / high_w - 1.0
                out[f"dist_from_low_{w}m"] = close / low_w - 1.0
        return out


def _returns(close: np.ndarray) -> np.ndarray:
    """Per-bar close-to-close return matrix ``close[t]/close[t-1] - 1`` over the trailing buffer; the first
    column (no prior bar) is NaN. Matches ``close/close.shift(1) - 1``."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close[:, 1:] / close[:, :-1] - 1.0
    return np.concatenate([np.full((close.shape[0], 1), np.nan), ret], axis=1)


_FOUR_LN2 = 2.772588722239781
_MOMENT_MIN_VAR = 1e-12


def _close_at_lag(close: np.ndarray, minute: np.ndarray, now_epoch: int, w: int) -> np.ndarray:
    """The per-symbol close at EXACTLY ``now_epoch − w`` minutes — the legacy ``LagSpec(minutes=w)`` strict
    TIME-lag (== ``base.lagged``): NaN where no bar is stamped at that exact minute (NOT the nearest / w-th
    prior PRESENT bar). ``close``/``minute`` are the rolled trailing matrices (oldest→newest; empty slots -1).
    """
    target = now_epoch - w * 60
    match = minute == target
    n_sym = close.shape[0]
    out = np.full(n_sym, np.nan)
    has = match.any(axis=1)
    if has.any():
        idx = np.argmax(match, axis=1)  # the one slot per symbol carrying the lagged minute
        out[has] = close[np.arange(n_sym)[has], idx[has]]
    return out


class PriceReturnsClean:
    """PRICE: simple + log close-to-close return over each window — ret_{w}m = close/close_{-w} − 1,
    log_ret_{w}m = ln(close/close_{-w}). The lag is a STRICT TIME-lag (legacy ``LagSpec(minutes=w)`` ==
    ``base.lagged``): close as of EXACTLY T − w minutes, NULL when that exact minute is absent — NOT the w-th
    prior PRESENT bar (which a sparse symbol would diverge on). Legacy: ``PriceReturnGroup`` (StatefulGroup).
    """

    name = "price_returns"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 45, 60, 90, 120, 180)
    feature_names = tuple(f"ret_{w}m" for w in _WINDOWS) + tuple(f"log_ret_{w}m" for w in _WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        minute = window.trailing_minute()
        latest = window.latest("close")
        now_epoch = window.minute_epoch
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            prior = _close_at_lag(close, minute, now_epoch, w)  # strict time-lag: close at exactly T−w
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = latest / prior
                out[f"ret_{w}m"] = ratio - 1.0
                out[f"log_ret_{w}m"] = np.log(ratio)
        return out


class DistributionClean:
    """DISTRIBUTION: per window of one-minute returns — ret_skew (m3/m2^1.5), ret_kurt (excess, m4/m2²−3),
    downside_vol / upside_vol (RMS of the negative / positive returns). Central moments from masked power sums;
    skew/kurt defined only when n≥3 and m2 > 1e-12 (variance floor against cancellation noise). Legacy:
    ``DistributionGroup`` (ReductionGroup, distribution.py)."""

    name = "distribution"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
    feature_names = tuple(
        f"{prefix}_{w}m"
        for w in _WINDOWS
        for prefix in ("ret_skew", "ret_kurt", "downside_vol", "upside_vol")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        ret = _returns(close)
        dn2 = np.where(ret < 0.0, ret * ret, 0.0)
        up2 = np.where(ret > 0.0, ret * ret, 0.0)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy sum_ = rolling_sum_by("minute")): the central-moment power sums are over the
            # return bars in the last w minutes, not the last w positional slots. The moments are non-linear but
            # built from LINEAR power sums (s1..s4) over the time window — the faithful decomposition.
            in_window = np.isfinite(window.trailing_time("close", w))
            rw = np.where(in_window, ret, np.nan)
            mask = np.isfinite(rw)
            n = mask.sum(axis=1).astype(np.float64)
            rf = np.where(mask, rw, 0.0)
            s1 = rf.sum(axis=1)
            s2 = (rf * rf).sum(axis=1)
            s3 = (rf * rf * rf).sum(axis=1)
            s4 = (rf * rf * rf * rf).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = s1 / n
                m2 = s2 / n - mean * mean
                m3 = s3 / n - 3.0 * mean * (s2 / n) + 2.0 * mean**3
                m4 = s4 / n - 4.0 * mean * (s3 / n) + 6.0 * mean * mean * (s2 / n) - 3.0 * mean**4
                defined = (n >= 3.0) & (m2 > _MOMENT_MIN_VAR)
                out[f"ret_skew_{w}m"] = np.where(defined, m3 / m2**1.5, np.nan)
                out[f"ret_kurt_{w}m"] = np.where(defined, m4 / (m2 * m2) - 3.0, np.nan)
                dn2_w = _row_sum(np.where(in_window, dn2, np.nan))
                up2_w = _row_sum(np.where(in_window, up2, np.nan))
                out[f"downside_vol_{w}m"] = np.where(
                    n >= 2.0, np.sqrt(np.clip(dn2_w / n, 0.0, None)), np.nan
                )
                out[f"upside_vol_{w}m"] = np.where(n >= 2.0, np.sqrt(np.clip(up2_w / n, 0.0, None)), np.nan)
        return out


class PriceVolumeClean:
    """PRICE_VOLUME: per window — vwap_deviation (close/vwap−1), up/down_volume_ratio, volume_delta,
    buying_pressure (volume-weighted money-flow), pv_correlation (return-vs-volume corr), obv_slope (OLS slope
    of on-balance-volume on time, normalized by mean window volume). Legacy: ``PriceVolumeGroup`` — the keystone
    windowed group, 7 features × 10 windows.

    TIME-windowed (legacy ``rolling_*_by("minute","{w}m")``): every windowed reduction is masked to the bars
    whose minute is in the last ``w`` minutes (``trailing_time``), NOT the last ``w`` positional slots — on a
    sparse symbol the two diverge. The obv_slope OLS regresses on the rebased real-minute axis (a ``kind="time"``
    regression), not a positional arange. Validated cell-for-cell vs legacy BATCH ``compute()`` on dense+sparse.
    """

    name = "price_volume"
    input_cols = ("high", "low", "close", "volume")
    _WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
    feature_names = tuple(
        f"{prefix}_{w}m"
        for w in _WINDOWS
        for prefix in (
            "vwap_deviation",
            "up_volume_ratio",
            "down_volume_ratio",
            "volume_delta",
            "buying_pressure",
            "pv_correlation",
            "obv_slope",
        )
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        high = window.trailing("high")
        low = window.trailing("low")
        close = window.trailing("close")
        volume = window.trailing("volume")
        latest_close = window.latest("close")
        # per-bar one-minute return (first column NaN, no prior bar)
        ret = _returns(close)
        # money-flow multiplier (2c−h−l)/(h−l), 0 when range is 0; weighted by volume
        rng = high - low
        with np.errstate(invalid="ignore", divide="ignore"):
            mfm = np.where(rng > 0.0, (2.0 * close - high - low) / rng, 0.0)
        mfv = mfm * volume
        cv = close * volume
        up_vol = np.where(ret > 0.0, volume, 0.0)
        dn_vol = np.where(ret < 0.0, volume, 0.0)
        # signed volume + on-balance-volume cumulative across the trailing buffer (NaN ret -> 0 signed). OBV
        # stays cumulative-from-buffer-start; only the per-window OLS over it is TIME-windowed (below).
        signed = np.where(ret > 0.0, volume, np.where(ret < 0.0, -volume, 0.0))
        signed = np.where(np.isfinite(signed), signed, 0.0)
        obv = np.cumsum(signed, axis=1)
        # the obv_slope OLS is a kind="time" regression: x = the ACTUAL MINUTE (frame-relative), NOT a positional
        # arange — on a sparse symbol the minute gaps make the slope differ. REBASE to the earliest present
        # minute so the operands stay small (raw epoch-minutes ~1e7 → (Σx)² ~1e15 → catastrophic cancellation in
        # denom_x → spurious floor reject). Empty slots NaN; the per-window time mask then restricts to (T−w, T].
        time_axis = _rebased_minute_axis(window.trailing_minute())
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy rolling_*_by("minute","{w}m")): keep only bars whose minute is within the last
            # w minutes, the rest NaN — a sparse symbol's window is bounded by TIME, not slot count.
            in_window = np.isfinite(window.trailing_time("close", w))
            vol_w = _row_sum(np.where(in_window, volume, np.nan))
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = _row_sum(np.where(in_window, cv, np.nan)) / vol_w
                out[f"vwap_deviation_{w}m"] = np.where(vol_w > 0.0, latest_close / vwap - 1.0, np.nan)
                up_w = _row_sum(np.where(in_window, up_vol, np.nan))
                dn_w = _row_sum(np.where(in_window, dn_vol, np.nan))
                out[f"up_volume_ratio_{w}m"] = np.where(vol_w > 0.0, up_w / vol_w, np.nan)
                out[f"down_volume_ratio_{w}m"] = np.where(vol_w > 0.0, dn_w / vol_w, np.nan)
                out[f"volume_delta_{w}m"] = np.where(vol_w > 0.0, (up_w - dn_w) / vol_w, np.nan)
                out[f"buying_pressure_{w}m"] = np.where(
                    vol_w > 0.0, _row_sum(np.where(in_window, mfv, np.nan)) / vol_w, np.nan
                )
            out[f"pv_correlation_{w}m"] = _row_corr(
                np.where(in_window, ret, np.nan), np.where(in_window, volume, np.nan)
            )
            mean_vol, _ = _row_mean(np.where(in_window, volume, np.nan))
            slope = _row_ols_slope(np.where(in_window, time_axis, np.nan), np.where(in_window, obv, np.nan))
            with np.errstate(invalid="ignore", divide="ignore"):
                out[f"obv_slope_{w}m"] = np.where(mean_vol > 0.0, slope / mean_vol, np.nan)
        return out


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
        # TIME windows (legacy std_/mean_ = rolling_*_by("minute")): reduce over the bars in the last w minutes,
        # not the last w positional slots — a sparse symbol diverges otherwise.
        for w in self._VOL_WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            out[f"realized_vol_{w}m"] = _row_std(np.where(in_window, ret, np.nan))
        for w in self._RANGE_WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            mean_hl2, _ = _row_mean(np.where(in_window, hl2, np.nan))
            with np.errstate(invalid="ignore"):
                out[f"parkinson_vol_{w}m"] = np.sqrt(np.clip(mean_hl2 / _FOUR_LN2, 0.0, None))
        return out


class LiquidityClean:
    """LIQUIDITY (Layer B): amihud_illiq_{w}m = mean(|return|/dollar-volume), undefined (NaN, excluded) on a
    non-positive dollar minute; roll_spread_{w}m = 2·sqrt(−cov(Δp, Δp_lag))/close when that autocov < 0 else 0;
    kyle_lambda_{w}m = OLS slope of Δp on signed_volume. Legacy: ``LiquidityGroup`` (ReductionGroup)."""

    name = "liquidity"
    input_cols = ("close", "volume", "signed_volume")
    _WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("amihud_illiq", "roll_spread", "kyle_lambda")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        volume = window.trailing("volume")
        signed_volume = window.trailing("signed_volume")
        latest_close = window.latest("close")
        # Δp = close - close_{-1} (first column NaN); Δp_lag = close_{-1} - close_{-2}.
        dp = np.full_like(close, np.nan)
        dp[:, 1:] = close[:, 1:] - close[:, :-1]
        dp_lag = np.full_like(close, np.nan)
        dp_lag[:, 2:] = close[:, 1:-1] - close[:, :-2]
        # amihud per-bar |return|/dollar, NaN on dollar<=0 (excluded from the window mean on both paths).
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = close[:, 1:] / close[:, :-1] - 1.0
            ret_full = np.concatenate([np.full((close.shape[0], 1), np.nan), ret], axis=1)
            dollar = close * volume
            amihud = np.where(dollar > 0.0, np.abs(ret_full) / dollar, np.nan)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy mean_/sum_ = rolling_*_by("minute")): the last w minutes, not w slots.
            in_window = np.isfinite(window.trailing_time("close", w))
            out[f"amihud_illiq_{w}m"], _ = _row_mean(np.where(in_window, amihud, np.nan))
            cov = _row_cov(np.where(in_window, dp, np.nan), np.where(in_window, dp_lag, np.nan))
            with np.errstate(invalid="ignore"):
                # Roll = 0 unless the autocov is strictly negative. Legacy polars: cov is null when n<2, and
                # ``when(null < 0)`` is null → falls to ``.otherwise(0.0)`` — so a too-short window emits 0.0,
                # NOT NaN (roll_spread is never NaN once a row exists; nan_policy=warmup only drops empty rows).
                out[f"roll_spread_{w}m"] = np.where(
                    cov < 0.0, 2.0 * np.sqrt(-np.where(cov < 0.0, cov, 0.0)) / latest_close, 0.0
                )
            out[f"kyle_lambda_{w}m"] = _row_ols_slope(
                np.where(in_window, signed_volume, np.nan), np.where(in_window, dp, np.nan)
            )
        return out


class QuoteSpreadClean:
    """QUOTE_SPREAD (Layer B): point-in-time last-minute spread/imbalance/depth + trailing means of spread and
    imbalance over each window. ``spread_bps_1m``/``quote_imbalance_1m`` = latest; ``book_depth_1m`` =
    latest(bid_size+ask_size); ``spread_bps_{w}m``/``quote_imbalance_{w}m`` = trailing means. nan_policy=sparse
    (absent quote minute -> NaN, which the masked mean / latest already yield). Legacy: ``QuoteSpreadGroup``.
    """

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
            # TIME window (legacy mean_ = rolling_mean_by("minute")): the last w minutes of quote bars. Mask on
            # the spread column's own presence (a quote-minute is present iff it has a spread reading).
            in_window = np.isfinite(window.trailing_time("mean_spread_bps", w))
            out[f"spread_bps_{w}m"], _ = _row_mean(np.where(in_window, spread, np.nan))
            out[f"quote_imbalance_{w}m"], _ = _row_mean(np.where(in_window, imbalance, np.nan))
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
            # TIME window (legacy mean_ = rolling_mean_by("minute")): the last w minutes, not w positional slots.
            in_window = np.isfinite(window.trailing_time("close", w))
            gk_mean, _ = _row_mean(np.where(in_window, gk_var, np.nan))
            rs_mean, _ = _row_mean(np.where(in_window, rs_var, np.nan))
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
            # TIME windows (legacy mean_ = rolling_mean_by("minute")): the last recent / trailing minutes.
            num, _ = _row_mean(np.where(np.isfinite(window.trailing_time("close", recent)), rng, np.nan))
            denom, _ = _row_mean(np.where(np.isfinite(window.trailing_time("close", trailing)), rng, np.nan))
            # Guard 2: denom is a mean of non-negative terms — sign-robust, denom>0 is sufficient. A
            # flat/zero-range trailing window -> NaN. is_finite backstop folds any stray non-finite to NaN.
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(denom > 0.0, num / denom, np.nan)
            out[f"range_expansion_{recent}_{trailing}m"] = np.where(np.isfinite(ratio), ratio, np.nan)
        return out


class MomentumClean:
    """MOMENTUM: per window of one-minute returns — up_ratio_{w}m = fraction of the trailing w minutes with a
    positive 1m return; mean_abs_ret_{w}m = mean |1m return| (choppiness). Both are TIME-windowed means of a
    per-bar quantity (the 1m return = close/prior-present-close − 1). Legacy: ``MomentumGroup`` (ReductionGroup).
    """

    name = "momentum"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
    feature_names = tuple(f"up_ratio_{w}m" for w in _WINDOWS) + tuple(f"mean_abs_ret_{w}m" for w in _WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        ret = _returns(close)  # per-bar 1m return vs the prior PRESENT bar (positional shift(1))
        up = np.where(ret > 0.0, 1.0, 0.0)  # 1 on an up-minute (matches (ret>0).cast(Float64))
        abs_ret = np.abs(ret)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy mean_ = rolling_mean_by("minute")): the mean is over the return bars in the
            # last w minutes. ``up``/``abs_ret`` are NaN where ret is NaN (the leading no-prior-bar column),
            # which the masked mean drops — matching the legacy reduction over present returns.
            in_window = np.isfinite(window.trailing_time("close", w)) & np.isfinite(ret)
            out[f"up_ratio_{w}m"], _ = _row_mean(np.where(in_window, up, np.nan))
            out[f"mean_abs_ret_{w}m"], _ = _row_mean(np.where(in_window, abs_ret, np.nan))
        return out


class EfficiencyClean:
    """MOMENTUM: Kaufman path-efficiency per window — efficiency_ratio_{w}m = |net change| / total absolute
    travel; directional_efficiency_{w}m = signed (net change / travel) in [-1,1]. MIXED semantics: the travel
    ``path`` is a TIME-windowed sum of |1m move|, but the net-change reference ``close.shift(w)`` is a
    POSITIONAL w-bar lookback (the w-th prior PRESENT bar) — verified against legacy on a sparse symbol. Legacy:
    ``EfficiencyGroup`` (ReductionGroup)."""

    name = "efficiency"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)
    feature_names = tuple(f"efficiency_ratio_{w}m" for w in _WINDOWS) + tuple(
        f"directional_efficiency_{w}m" for w in _WINDOWS
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        # |minute-to-minute move| per bar (NaN leading column, no prior bar) — the legacy ``step`` reduction.
        step = np.full_like(close, np.nan)
        step[:, 1:] = np.abs(close[:, 1:] - close[:, :-1])
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # path = TIME-windowed sum of |step| over the last w minutes.
            in_window = np.isfinite(window.trailing_time("close", w))
            path = _row_sum(np.where(in_window, step, np.nan))
            # net change = close[T] − close.shift(w) = the w-th prior PRESENT bar (POSITIONAL, legacy pt_(l{w})).
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(n_sym, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(path > 0.0, (latest_close - prior) / path, np.nan)
            out[f"efficiency_ratio_{w}m"] = np.abs(ratio)
            out[f"directional_efficiency_{w}m"] = ratio
        return out


def _positional_shift(mat: np.ndarray, k: int) -> np.ndarray:
    """Shift a ``(n, window)`` trailing matrix RIGHT by ``k`` columns (positional ``shift(k).over(symbol)``):
    column t holds the value from ``k`` present-bars earlier; the first ``k`` columns become NaN. Used for the
    lagged-return regressors / prior-return references that legacy builds with ``close.shift(k)``."""
    out = np.full_like(mat, np.nan)
    if k < mat.shape[1]:
        out[:, k:] = mat[:, : mat.shape[1] - k]
    return out


class ReturnDynamicsClean:
    """MOMENTUM: return-structure features. autocorr_{1,2}_{w}m = lag-1/lag-2 autocorrelation of the 1m return
    over the TIME window (corr of ret vs its positional lag, in [-1,1]); ret_accel_{w}m = trailing-w return
    minus the prior-w return. MIXED: the autocorr is a TIME-windowed corr of value-on-(positional-lagged-value),
    but ret_accel's references ``close.shift(w)``/``shift(2w)`` are POSITIONAL w/2w-bar lookbacks. Legacy:
    ``ReturnDynamicsGroup`` (ReductionGroup)."""

    name = "return_dynamics"
    input_cols = ("close",)
    _AUTOCORR_WINDOWS: tuple[int, ...] = (10, 15, 30, 60, 120)
    _ACCEL_WINDOWS: tuple[int, ...] = (5, 10, 15, 30, 60)
    feature_names = (
        tuple(f"autocorr_1_{w}m" for w in _AUTOCORR_WINDOWS)
        + tuple(f"autocorr_2_{w}m" for w in _AUTOCORR_WINDOWS)
        + tuple(f"ret_accel_{w}m" for w in _ACCEL_WINDOWS)
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        ret = _returns(close)  # ret[t] = close[t]/close[t-1] − 1 (positional 1m return)
        ret_lag1 = _positional_shift(ret, 1)  # ret at t−1
        ret_lag2 = _positional_shift(ret, 2)  # ret at t−2
        out: dict[str, np.ndarray] = {}
        for w in self._AUTOCORR_WINDOWS:
            # TIME window (legacy corr_ = rolling OLS over rolling_sum_by("minute")): corr over the bars in the
            # last w minutes where BOTH the return and its lag are present.
            in_window = np.isfinite(window.trailing_time("close", w))
            out[f"autocorr_1_{w}m"] = _row_corr(
                np.where(in_window, ret_lag1, np.nan), np.where(in_window, ret, np.nan)
            )
            out[f"autocorr_2_{w}m"] = _row_corr(
                np.where(in_window, ret_lag2, np.nan), np.where(in_window, ret, np.nan)
            )
        for w in self._ACCEL_WINDOWS:
            # ret_accel = (close[T]/close.shift(w) − 1) − (close.shift(w)/close.shift(2w) − 1) — POSITIONAL
            # w/2w-bar lookbacks (legacy pt_(l{w})/pt_(l{2w})).
            lw = close[:, -(w + 1)] if close.shape[1] > w else np.full(n_sym, np.nan)
            l2w = close[:, -(2 * w + 1)] if close.shape[1] > 2 * w else np.full(n_sym, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                recent = latest_close / lw - 1.0
                prior = lw / l2w - 1.0
                out[f"ret_accel_{w}m"] = recent - prior
        return out


class MomentumConsistencyClean:
    """MOMENTUM: path-consistency from the 1m return signs over each window. consistent_direction_{w}m =
    fraction of the window's returns whose sign matches the net w-move (0.5 when net flat); reversal_count_{w}m =
    sign-flips between consecutive returns / w; momentum_acceleration_{w}m = (recent-half mean − older-half mean)
    × 100. All additive TIME-windowed sums of short-lag sign indicators; the net-move reference ``close.shift(w)``
    is a POSITIONAL w-bar lookback. Legacy: ``MomentumConsistencyGroup`` (ReductionGroup)."""

    name = "momentum_consistency"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
    feature_names = tuple(
        f"{prefix}_{w}m"
        for w in _WINDOWS
        for prefix in ("consistent_direction", "reversal_count", "momentum_acceleration")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        ret = _returns(close)  # close[t]/close[t-1] − 1; NaN where the immediate prior minute is absent
        ret_prev = _positional_shift(ret, 1)
        finite_ret = np.isfinite(ret)
        up = np.where(finite_ret & (ret > 0.0), 1.0, np.where(finite_ret, 0.0, np.nan))
        down = np.where(finite_ret & (ret < 0.0), 1.0, np.where(finite_ret, 0.0, np.nan))
        has_ret = np.where(finite_ret, 1.0, np.nan)
        # a reversal: both returns present, nonzero, and opposite sign.
        flip_bool = (
            finite_ret
            & np.isfinite(ret_prev)
            & (ret != 0.0)
            & (ret_prev != 0.0)
            & ((ret > 0.0) != (ret_prev > 0.0))
        )
        flip = np.where(finite_ret, flip_bool.astype(np.float64), np.nan)
        ret_filled = np.where(finite_ret, ret, np.nan)  # present-only 1m return (sum excludes absent)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            half = w // 2
            full_mask = np.isfinite(window.trailing_time("close", w))
            half_mask = np.isfinite(window.trailing_time("close", half))
            n_ret = _row_sum(np.where(full_mask, has_ret, np.nan))
            up_w = _row_sum(np.where(full_mask, up, np.nan))
            down_w = _row_sum(np.where(full_mask, down, np.nan))
            flip_w = _row_sum(np.where(full_mask, flip, np.nan))
            # net move over the window = close[T] − close.shift(w) (POSITIONAL w-bar lookback). NaN until the
            # w-ago bar exists.
            lw = close[:, -(w + 1)] if close.shape[1] > w else np.full(n_sym, np.nan)
            net = latest_close - lw
            with np.errstate(invalid="ignore", divide="ignore"):
                frac = np.where(net > 0.0, up_w / n_ret, np.where(net < 0.0, down_w / n_ret, 0.5))
                defined_dir = (n_ret > 0.0) & np.isfinite(net)
                out[f"consistent_direction_{w}m"] = np.where(defined_dir, frac, np.nan)
                out[f"reversal_count_{w}m"] = np.where(n_ret > 0.0, flip_w / float(w), np.nan)
                # acceleration: recent half-window mean − older (full − recent) mean, ×100.
                recent_n = _row_sum(np.where(half_mask, has_ret, np.nan))
                recent_sum = _row_sum(np.where(half_mask, ret_filled, np.nan))
                older_n = n_ret - recent_n
                older_sum = _row_sum(np.where(full_mask, ret_filled, np.nan)) - recent_sum
                recent_mean = np.where(recent_n > 0.0, recent_sum / recent_n, np.nan)
                older_mean = np.where(older_n > 0.0, older_sum / older_n, np.nan)
                out[f"momentum_acceleration_{w}m"] = (recent_mean - older_mean) * 100.0
        return out


def _running_extreme(mat: np.ndarray, kind: str) -> np.ndarray:
    """Per-row running cum-max / cum-min over a ``(n, k)`` TIME-windowed close matrix (oldest→newest), ignoring
    NaN (absent / out-of-window bars do not reset the extreme). ``kind`` is "max" or "min". Cells that are NaN
    stay NaN in the output (no extreme defined there yet)."""
    fill = -np.inf if kind == "max" else np.inf
    filled = np.where(np.isfinite(mat), mat, fill)
    running = (
        np.maximum.accumulate(filled, axis=1) if kind == "max" else np.minimum.accumulate(filled, axis=1)
    )
    return np.where(np.isfinite(mat), running, np.nan)


_DRAW_MIN_POINTS = 2  # a path excursion needs ≥2 closes (one bar cannot draw up or down)


class DrawRangeClean:
    """VOLATILITY (path-shape): over the trailing w minutes, max draw-down |maxdd| + max draw-up maxdu of the
    close path. max_drawdown_{w}m = |min_t(close/running_max − 1)|, max_drawup_{w}m = max_t(close/running_min −
    1), draw_range_{w}m = their sum. The running cum-max/cum-min are anchored at the window's earliest in-window
    bar (TIME window, close>0 only). NULL on a window with <2 closes. Legacy: ``DrawRangeGroup``."""

    name = "draw_range"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (60,)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("draw_range", "max_drawup", "max_drawdown")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window of closes (legacy rolling period "{w}m"); Guard 2: close>0 only (a non-positive bar
            # would corrupt the running extreme — excluded identically to the legacy pre-filter).
            close_w = window.trailing_time("close", w)
            close_w = np.where(close_w > 0.0, close_w, np.nan)
            n = np.isfinite(close_w).sum(axis=1)
            run_max = _running_extreme(close_w, "max")
            run_min = _running_extreme(close_w, "min")
            with np.errstate(invalid="ignore", divide="ignore"):
                drawdown_path = close_w / run_max - 1.0  # ≤ 0 per bar
                drawup_path = close_w / run_min - 1.0  # ≥ 0 per bar
            maxdd = _row_nanmin(drawdown_path)  # the deepest (most negative) drawdown
            maxdu = _row_nanmax(drawup_path)  # the highest drawup
            warm = n >= _DRAW_MIN_POINTS
            drawdown_leg = np.where(warm, -maxdd, np.nan)  # magnitude (≥0)
            drawup_leg = np.where(warm, maxdu, np.nan)
            total = np.where(warm, -maxdd + maxdu, np.nan)
            # is_finite backstop (matches legacy _finite): any stray non-finite → NaN.
            out[f"draw_range_{w}m"] = np.where(np.isfinite(total), total, np.nan)
            out[f"max_drawup_{w}m"] = np.where(np.isfinite(drawup_leg), drawup_leg, np.nan)
            out[f"max_drawdown_{w}m"] = np.where(np.isfinite(drawdown_leg), drawdown_leg, np.nan)
        return out


_VOL_STD_REL_EPS = 1e-6  # volume_zscore relative-std guard: std must be a non-trivial fraction of the mean


class VolumeClean:
    """VOLUME: dollar_volume_1m = latest close·volume; volume_zscore_{w}m = (volT − mean)/std over the trailing
    w minutes (NULL on a near-constant-volume window — the RELATIVE std guard std > 1e-6·|mean|, not a bare
    std>0); volume_ratio_{w}m = volT/mean. Mean/std are TIME-windowed (ddof=1). Legacy: ``VolumeGroup``."""

    name = "volume"
    input_cols = ("close", "volume")
    _WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180)
    feature_names = ("dollar_volume_1m",) + tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("volume_zscore", "volume_ratio")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        volume = window.trailing("volume")
        latest_volume = window.latest("volume")
        latest_close = window.latest("close")
        with np.errstate(invalid="ignore"):
            out: dict[str, np.ndarray] = {"dollar_volume_1m": latest_close * latest_volume}
        for w in self._WINDOWS:
            # TIME window (legacy mean_/std_ = rolling_*_by("minute")): the mean/std over the last w minutes.
            in_window = np.isfinite(window.trailing_time("close", w))
            masked_vol = np.where(in_window, volume, np.nan)
            mean_w, _ = _row_mean(masked_vol)
            std_w = _row_std(masked_vol)
            with np.errstate(invalid="ignore", divide="ignore"):
                # RELATIVE std guard: a near-constant-volume window (std ~1e-9 of a ~1e6 mean) → NULL z-score,
                # not a blown-up ratio. NULL also during warm-up (std NaN, <2 samples).
                zscore = (latest_volume - mean_w) / std_w
                out[f"volume_zscore_{w}m"] = np.where(
                    std_w > _VOL_STD_REL_EPS * np.abs(mean_w), zscore, np.nan
                )
                out[f"volume_ratio_{w}m"] = latest_volume / mean_w
        return out


class ResidualAnalysisClean:
    """TREND_QUALITY: residual_std_{w}m = std of the OLS residuals around the trailing-w close-vs-time trend, as
    a percent of mean price (how tightly price hugs its trend line). A time-OLS (x = rebased minute) — the SSR
    via the legacy centered-sum algebra, guarded n≥4 & sxx_c>0 & resid_var>(1e-6·ȳ)². Legacy:
    ``ResidualAnalysisGroup``."""

    name = "residual_analysis"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
    feature_names = tuple(f"residual_std_{w}m" for w in _WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        time_axis = _rebased_minute_axis(window.trailing_minute())
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            x = np.where(in_window, time_axis, np.nan)
            y = np.where(in_window, close, np.nan)
            _slope, _r2, resid_std, _mean_y, _n = _time_ols_slope_r2_resid(x, y)
            out[f"residual_std_{w}m"] = resid_std
        return out


_CM_SLOPE_CAP = 0.001
_CM_SLOPE_WEIGHT = 0.4
_CM_R2_WEIGHT = 0.3
_CM_RESID_WEIGHT = 0.3
_CM_RESID_SCALE = 0.5
_CM_FLAG_SLOPE_MIN = 0.0002
_CM_FLAG_R2_MIN = 0.7
_CM_FLAG_RESID_MAX = 0.3


class CleanMomentumClean:
    """TREND_QUALITY: clean_momentum_score_{w}m = a 0-1 composite blending normalized slope magnitude, R², and
    low residuals of the trailing-w close-vs-time OLS; momentum_quality_flag_{w}m = 1.0 when the trend is a
    high-quality setup (|slope|>thr AND R²>0.7 AND resid_std<0.3). A time-OLS (x = rebased minute). The
    residual-std term is built from the SAME OLS bundle as residual_analysis (std_close²·(n-1)·(1-r2)/n form,
    here via the shared SSR). NULL where resid_std is undefined. Legacy: ``CleanMomentumScoreGroup``."""

    name = "clean_momentum"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("clean_momentum_score", "momentum_quality_flag")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        time_axis = _rebased_minute_axis(window.trailing_minute())
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            x = np.where(in_window, time_axis, np.nan)
            y = np.where(in_window, close, np.nan)
            slope, r2, _ssr_resid, mean_y, _n = _time_ols_slope_r2_resid(x, y)
            # clean_momentum's resid_std is its OWN formula (NOT residual_analysis's SSR stat): std_close² ·
            # (n−1)·(1−r2)/n, √, /mean·100 — guarded only by std_close (n≥2) + r2 being defined, a LOOSER gate
            # than the SSR stat's n≥4/floor (so it's defined on cells the SSR resid is not). mean/std over the
            # SAME time window.
            mean_close, _ = _row_mean(np.where(in_window, close, np.nan))
            std_close = _row_std(np.where(in_window, close, np.nan))
            n_window = _row_sum(np.where(in_window, 1.0, np.nan))
            with np.errstate(invalid="ignore", divide="ignore"):
                var_resid = std_close**2 * (n_window - 1.0) * (1.0 - r2) / n_window
                resid_std = np.sqrt(np.clip(var_resid, 0.0, None)) / mean_close * 100.0
                abs_slope = np.abs(slope / mean_y)  # fractional move per minute (legacy _norm_slope)
                slope_score = np.clip(abs_slope / _CM_SLOPE_CAP, None, 1.0) * _CM_SLOPE_WEIGHT
                r2_score = r2 * _CM_R2_WEIGHT
                resid_score = np.clip(1.0 - resid_std / _CM_RESID_SCALE, 0.0, None) * _CM_RESID_WEIGHT
                score = slope_score + r2_score + resid_score
                flag = (
                    (abs_slope > _CM_FLAG_SLOPE_MIN)
                    & (r2 > _CM_FLAG_R2_MIN)
                    & (resid_std < _CM_FLAG_RESID_MAX)
                )
            # the whole group keys on resid_std being defined (legacy: when(resid_std.is_null()).then(None)).
            resid_undefined = ~np.isfinite(resid_std)
            out[f"clean_momentum_score_{w}m"] = np.where(resid_undefined, np.nan, score)
            out[f"momentum_quality_flag_{w}m"] = np.where(resid_undefined, np.nan, flag.astype(np.float64))
        return out


def _windowed_longest_streak(rl: np.ndarray, ret: np.ndarray, in_window: np.ndarray, w: int) -> np.ndarray:
    """longest_streak_{w}m = max over the in-window present-return bars of min(run_length, position_in_window),
    normalized by w; NULL when fewer than 2 returns in the window. The position cap stops a run that started
    BEFORE the window from counting its pre-window bars. ``rl`` is the global run length per bar; a present-
    return in-window bar is one with a finite return AND in the time window."""
    n_sym, k = ret.shape
    bar_in = in_window & np.isfinite(ret)  # the present-return bars inside the time window
    position = np.cumsum(
        bar_in, axis=1
    )  # 1-based position among in-window present-return bars (0 where not)
    capped = np.where(bar_in, np.minimum(rl, position), 0)
    streak = capped.max(axis=1).astype(np.float64)
    n_ret = bar_in.sum(axis=1)
    return np.where(n_ret >= 2, streak / float(w), np.nan)


class MomentumRunClean:
    """TREND_QUALITY: residual_skew_{w}m = window-local standardized 3rd moment of the close-on-time OLS
    residuals (asymmetry of the deviations); longest_streak_{w}m = longest run of consecutive same-direction
    1m returns over the window, capped to the window and normalized by w. Legacy: ``MomentumRunGroup``
    (FeatureGroup). The skew uses a window-LOCAL fit on the rebased-minute axis; the streak is a run-length path
    statistic (global run length capped by in-window position)."""

    name = "momentum_run"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("residual_skew", "longest_streak")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        time_axis = _rebased_minute_axis(window.trailing_minute())
        ret = _returns(close)
        rl = _global_run_length(ret)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            x = np.where(in_window, time_axis, np.nan)
            y = np.where(in_window, close, np.nan)
            out[f"residual_skew_{w}m"] = _time_ols_resid_skew(x, y)
            out[f"longest_streak_{w}m"] = _windowed_longest_streak(rl, ret, in_window, w)
        return out
