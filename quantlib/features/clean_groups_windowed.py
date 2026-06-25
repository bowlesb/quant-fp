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


class PriceReturnsClean:
    """PRICE: simple + log close-to-close return over each trailing window — ret_{w}m = close/close_{-w} − 1,
    log_ret_{w}m = ln(close/close_{-w}). The lag is POSITIONAL (the w-th prior present bar in the carried
    buffer), matching the engine's gap-safe trailing semantics. NaN until w+1 present bars (warm-up). Legacy:
    ``PriceReturnGroup`` (a StatefulGroup whose math is plain positional-lag returns, not a state machine).
    """

    name = "price_returns"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 45, 60, 90, 120, 180)
    feature_names = tuple(f"ret_{w}m" for w in _WINDOWS) + tuple(f"log_ret_{w}m" for w in _WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        latest = window.latest("close")
        n_sym = close.shape[0]
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # the w-th prior present bar is buffer column -(w+1); NaN if the symbol has < w+1 present bars.
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(n_sym, np.nan)
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
            rw = _trailing_window(ret, w)
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
                dn2_w = _windowed_sum(dn2, w)
                up2_w = _windowed_sum(up2, w)
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
        for w in self._VOL_WINDOWS:
            out[f"realized_vol_{w}m"] = _masked_std(ret, w)
        for w in self._RANGE_WINDOWS:
            mean_hl2, _ = _masked_mean(hl2, w)
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
            out[f"amihud_illiq_{w}m"], _ = _masked_mean(amihud, w)
            cov = _windowed_cov(dp, dp_lag, w)
            with np.errstate(invalid="ignore"):
                roll = np.where(cov < 0.0, 2.0 * np.sqrt(-cov) / latest_close, 0.0)
            # cov NaN (warm-up, <2 pairs) -> NaN; cov>=0 -> 0.0 (Roll defined as 0 on non-negative autocov).
            out[f"roll_spread_{w}m"] = np.where(np.isnan(cov), np.nan, roll)
            out[f"kyle_lambda_{w}m"] = _windowed_ols_slope(signed_volume, dp, w)
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
