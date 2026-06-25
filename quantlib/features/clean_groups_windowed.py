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


def _windowed_ols_slope(x_mat: np.ndarray, y_mat: np.ndarray, w: int) -> np.ndarray:
    """Trailing ``w``-window OLS slope of ``y`` on ``x`` per symbol, masked to bars where BOTH are finite —
    matching the legacy ``slope_`` reduction: slope = (n·Σxy − Σx·Σy) / (n·Σx² − (Σx)²), NaN where n<2 or
    var_x==0. ``x_mat``/``y_mat`` are ``(n_symbols, buffer)``."""
    xw = _trailing_window(x_mat, w)
    yw = _trailing_window(y_mat, w)
    mask = np.isfinite(xw) & np.isfinite(yw)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, xw, 0.0)
    yf = np.where(mask, yw, 0.0)
    sx = xf.sum(axis=1)
    sy = yf.sum(axis=1)
    sxx = (xf * xf).sum(axis=1)
    sxy = (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = n * sxy - sx * sy
        var_x = n * sxx - sx * sx
        slope = cov / var_x
    return np.where((n >= 2.0) & (var_x > 0.0), slope, np.nan)


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
    """Trailing ``w``-window Pearson correlation of ``x`` and ``y`` per symbol over bars where BOTH finite —
    matching the legacy ``corr_`` reduction: r = (n·Σxy−Σx·Σy)/sqrt((n·Σx²−(Σx)²)(n·Σy²−(Σy)²)), NaN where n<2
    or either variance is non-positive."""
    xw = _trailing_window(x_mat, w)
    yw = _trailing_window(y_mat, w)
    mask = np.isfinite(xw) & np.isfinite(yw)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, xw, 0.0)
    yf = np.where(mask, yw, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = n * sxy - sx * sy
        var_x = n * sxx - sx * sx
        var_y = n * syy - sy * sy
        corr = cov / np.sqrt(var_x * var_y)
    return np.where((n >= 2.0) & (var_x > 0.0) & (var_y > 0.0), corr, np.nan)


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


def _returns(close: np.ndarray) -> np.ndarray:
    """Per-bar close-to-close return matrix ``close[t]/close[t-1] - 1`` over the trailing buffer; the first
    column (no prior bar) is NaN. Matches ``close/close.shift(1) - 1``."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close[:, 1:] / close[:, :-1] - 1.0
    return np.concatenate([np.full((close.shape[0], 1), np.nan), ret], axis=1)


_FOUR_LN2 = 2.772588722239781


class PriceVolumeClean:
    """PRICE_VOLUME: per window — vwap_deviation (close/vwap−1), up/down_volume_ratio, volume_delta,
    buying_pressure (volume-weighted money-flow), pv_correlation (return-vs-volume corr), obv_slope (OLS slope
    of on-balance-volume on time, normalized by mean window volume). Legacy: ``PriceVolumeGroup`` — the keystone
    windowed group, 7 features × 10 windows. Built on the shared sum/corr/ols-slope kernels."""

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
        # signed volume + on-balance-volume cumulative across the trailing buffer (NaN ret -> 0 signed)
        signed = np.where(ret > 0.0, volume, np.where(ret < 0.0, -volume, 0.0))
        signed = np.where(np.isfinite(signed), signed, 0.0)
        obv = np.cumsum(signed, axis=1)
        # frame-relative time axis (origin-invariant; the slope is unaffected by the offset)
        time_axis = np.broadcast_to(np.arange(close.shape[1], dtype=np.float64), close.shape)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            vol_w = _windowed_sum(volume, w)
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = _windowed_sum(cv, w) / vol_w
                out[f"vwap_deviation_{w}m"] = np.where(vol_w > 0.0, latest_close / vwap - 1.0, np.nan)
                up_w = _windowed_sum(up_vol, w)
                dn_w = _windowed_sum(dn_vol, w)
                out[f"up_volume_ratio_{w}m"] = np.where(vol_w > 0.0, up_w / vol_w, np.nan)
                out[f"down_volume_ratio_{w}m"] = np.where(vol_w > 0.0, dn_w / vol_w, np.nan)
                out[f"volume_delta_{w}m"] = np.where(vol_w > 0.0, (up_w - dn_w) / vol_w, np.nan)
                out[f"buying_pressure_{w}m"] = np.where(vol_w > 0.0, _windowed_sum(mfv, w) / vol_w, np.nan)
            out[f"pv_correlation_{w}m"] = _windowed_corr(ret, volume, w)
            mean_vol, _ = _masked_mean(volume, w)
            slope = _windowed_ols_slope(time_axis, obv, w)
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
