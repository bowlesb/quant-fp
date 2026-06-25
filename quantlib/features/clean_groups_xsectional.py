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
from quantlib.features.clean_groups_windowed import _row_corr, _row_ols_slope, _row_std


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


def _daily_return_xsec(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Per-symbol ``w``-day return from the daily snapshot: ``_asof / close[w days back] − 1`` (newest daily
    column / w cols back). NaN where the symbol lacks w+1 daily bars. Used by the daily-horizon cross-sectional
    dispersion."""
    n_sym, n_days = daily_close.shape
    if n_days == 0:
        return np.full(n_sym, np.nan)
    asof = daily_close[:, -1]
    ref = daily_close[:, -(w + 1)] if n_days > w else np.full(n_sym, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return asof / ref - 1.0


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


def _polars_quantile(sorted_vals: np.ndarray, q: float) -> float:
    """polars' DEFAULT quantile (the legacy IQR's exact rule): ``sorted[floor(q·(n−1) + 0.5)]`` — round-HALF-UP
    of the fractional index. This is NOT numpy ``method='nearest'`` (round-half-to-EVEN) — they diverge exactly
    when ``q·(n−1)`` lands on x.5 (e.g. n=7, q=0.75 → 4.5 → polars index 5, numpy index 4). Matching polars
    exactly is parity-critical: the historical vectors were polars-computed, so a numpy-nearest IQR is
    train/serve-skewed."""
    n = len(sorted_vals)
    idx = int(np.floor(q * (n - 1) + 0.5))
    idx = max(0, min(n - 1, idx))
    return float(sorted_vals[idx])


def _xsec_std_iqr(returns: np.ndarray, present: np.ndarray) -> tuple[float, float]:
    """Cross-sectional std (ddof=1) + IQR (p75−p25, polars' default quantile rule) of the PRESENT+finite
    returns. Returns ``(std, iqr)``, NaN where fewer than 2 present-finite values. Broadcast by the caller.
    """
    masked = returns[present & np.isfinite(returns)]
    if masked.size < 2:
        return np.nan, np.nan
    std = float(np.std(masked, ddof=1))
    sorted_vals = np.sort(masked)
    iqr = _polars_quantile(sorted_vals, 0.75) - _polars_quantile(sorted_vals, 0.25)
    return std, iqr


class ReturnDispersionClean:
    """CROSS_SECTIONAL: per intraday horizon, the std + IQR of the universe's returns that minute — a
    market-wide scalar broadcast to every present symbol (high = stock-picking regime). present()-gated:
    only this-minute-present symbols enter the dispersion. Legacy: ``ReturnDispersionGroup``.

    NOTE: the legacy also ships daily-horizon dispersions (``_1d``/``_5d``) read from the settled daily
    snapshot via ``window.session['daily_close']`` (the cross-sectional std/IQR of the universe's w-day returns).
    """

    name = "return_dispersion"
    input_cols = ("close",)
    _MINUTE_WINDOWS: tuple[int, ...] = (5, 30, 60)
    _DAY_WINDOWS: tuple[int, ...] = (1, 5)
    feature_names = tuple(
        f"return_dispersion_{stat}_{w}m" for w in _MINUTE_WINDOWS for stat in ("std", "iqr")
    ) + tuple(f"return_dispersion_{stat}_{w}d" for w in _DAY_WINDOWS for stat in ("std", "iqr"))

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        present = window.present()
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        out: dict[str, np.ndarray] = {}
        for w in self._MINUTE_WINDOWS:
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(n_sym, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = latest_close / prior - 1.0
            std, iqr = _xsec_std_iqr(ret, present)
            # broadcast the market-wide scalar to every present symbol; absent → NaN (sparse).
            out[f"return_dispersion_std_{w}m"] = np.where(present, std, np.nan)
            out[f"return_dispersion_iqr_{w}m"] = np.where(present, iqr, np.nan)
        # daily horizons: cross-sectional std/IQR of the universe's w-day returns, from the daily snapshot.
        daily_close = window.session.get("daily_close")
        for w in self._DAY_WINDOWS:
            if daily_close is None:
                std_d, iqr_d = np.nan, np.nan
            else:
                daily_ret = _daily_return_xsec(daily_close, w)
                std_d, iqr_d = _xsec_std_iqr(daily_ret, present)
            out[f"return_dispersion_std_{w}d"] = np.where(present, std_d, np.nan)
            out[f"return_dispersion_iqr_{w}d"] = np.where(present, iqr_d, np.nan)
        return out


_SECTOR_MIN_PAIRS = 5  # legacy sector_beta MIN_PAIRS — fewer paired returns over the window → undefined fit
_SECTOR_BETA_MAX = 15.0  # |beta| above this → NULL (legacy BETA_MAX)


def _sector_equal_weight_returns(ret_mat: np.ndarray, sector: np.ndarray) -> np.ndarray:
    """Per-(minute, sector) equal-weight mean of the finite returns, broadcast back to each symbol's row →
    a ``(n_symbols, buffer)`` matrix where cell [i,t] = the mean return of symbol i's sector at minute t (over
    the symbols present+finite in that sector that minute). NaN where the symbol's return is NaN or its sector
    is empty that minute. ``sector`` is the per-symbol integer label (UNKNOWN handled by the caller)."""
    out = np.full_like(ret_mat, np.nan)
    for sec in np.unique(sector):
        rows = np.where(sector == sec)[0]
        block = ret_mat[rows]  # (n_in_sector, buffer)
        mask = np.isfinite(block)
        n = mask.sum(axis=0)  # present-finite count per minute in this sector
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(mask, block, 0.0).sum(axis=0) / n
        mean = np.where(n > 0, mean, np.nan)  # (buffer,)
        out[rows] = mean[None, :]  # broadcast the sector mean to each member row
    return out


def _windowed_sector_ols(
    own_ret: np.ndarray, sec_ret: np.ndarray, w: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """sector_beta's OLS of own return (y=``own_ret``) on sector return (x=``sec_ret``), paired where BOTH
    finite. Matches legacy ``_ols_from_sums`` EXACTLY: cov=sxy−sx·sy/n, var=sxx−sx²/n; defined = n≥MIN_PAIRS(5)
    & var_x>0 & var_y>0; beta NULL when |beta|>BETA_MAX(15); corr clipped to [-1,1]. ``w`` (legacy positional
    slice, kept for the helper's unit tests) takes the last ``w`` columns; if ``None`` the inputs are already
    TIME-windowed (the group masks bars outside the Δminute window to NaN) and the whole matrix is summed."""
    yw = _trailing_window(own_ret, w) if w is not None else own_ret
    xw = _trailing_window(sec_ret, w) if w is not None else sec_ret
    mask = np.isfinite(xw) & np.isfinite(yw)
    n = mask.sum(axis=1).astype(np.float64)
    xf = np.where(mask, xw, 0.0)
    yf = np.where(mask, yw, 0.0)
    sx, sy = xf.sum(axis=1), yf.sum(axis=1)
    sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sy / n
        var_x = sxx - sx * sx / n
        var_y = syy - sy * sy / n
        beta = cov / var_x
        corr = cov / (np.sqrt(var_x) * np.sqrt(var_y))
    defined = (n >= _SECTOR_MIN_PAIRS) & (var_x > 0.0) & (var_y > 0.0)
    beta_out = np.where(defined & (np.abs(beta) <= _SECTOR_BETA_MAX), beta, np.nan)
    corr_out = np.where(defined, np.clip(corr, -1.0, 1.0), np.nan)
    return beta_out, corr_out


def _trailing_window(mat: np.ndarray, w: int) -> np.ndarray:
    """Last ``w`` columns of a ``(n_symbols, buffer)`` matrix."""
    return mat[:, -w:]


def _buffer_returns(close: np.ndarray) -> np.ndarray:
    """Per-bar close-to-close return matrix over the trailing buffer; first column NaN (no prior bar)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close[:, 1:] / close[:, :-1] - 1.0
    return np.concatenate([np.full((close.shape[0], 1), np.nan), ret], axis=1)


def _sector_mean_vector(values: np.ndarray, sector: np.ndarray, present: np.ndarray) -> np.ndarray:
    """Per-symbol broadcast of its sector's equal-weight mean of ``values`` over the PRESENT+finite members of
    that sector this minute. ``(n_symbols,)``; NaN where the symbol's sector has no present member. Used by the
    per-window cross-sectional sector aggregates (sector_return)."""
    out = np.full(values.shape, np.nan)
    valid = present & np.isfinite(values)
    for sec in np.unique(sector):
        rows = sector == sec
        members = rows & valid
        if members.any():
            out[rows] = values[members].mean()
    return out


_RV_WINDOW = 30  # market_turbulence realized-vol window (minutes): std of 1m logrets over (T-RV_WINDOW, T]
_RV_MIN_OBS = 10  # min valid 1m logrets in the RV window for a defined per-symbol RV (else NaN)


def _value_at_lag(close: np.ndarray, minute: np.ndarray, now_epoch: int, w: int) -> np.ndarray:
    """The per-symbol close at EXACTLY ``now_epoch − w`` minutes (the legacy ``lagged(close, w)`` point
    lookup) — NaN where no bar is stamped at that exact minute. ``close``/``minute`` are the rolled trailing
    matrices (oldest→newest); ``minute`` empty slots are -1. This is a strict time-lag (a sparse symbol with no
    bar exactly ``w`` ago gets NaN, NOT its nearest bar) — the point-in-time semantics ``mkt_absret`` needs, not
    a window MASK."""
    target = now_epoch - w * 60
    match = minute == target  # (n_sym, window) — the slot(s) at the exact lagged minute
    n_sym = close.shape[0]
    out = np.full(n_sym, np.nan)
    has = match.any(axis=1)
    if has.any():
        # exactly one bar per symbol can carry a given minute-epoch (the ring stamps one per minute), so the
        # first (only) match per row is the value.
        idx = np.argmax(match, axis=1)
        out[has] = close[np.arange(n_sym)[has], idx[has]]
    return out


def _market_realized_vol(
    close: np.ndarray, minute: np.ndarray, present: np.ndarray, now_epoch: int
) -> np.ndarray:
    """Per-symbol trailing-``_RV_WINDOW`` realized vol at T = ddof=1 std of the 1m LOG returns over the window
    ``(T−_RV_WINDOW, T]``, defined with at least ``_RV_MIN_OBS`` valid returns (else NaN). A logret is valid
    ONLY across an EXACT one-minute step with both closes positive (a gap does not splice a multi-minute jump
    into the vol), matching legacy ``_realized_vol``. ``close``/``minute`` are the FULL rolled trailing matrices
    (oldest→newest, empty slots minute -1) — the logret at minute m needs close[m] AND close[m−1], so the close
    a step BEFORE the left edge (at T−_RV_WINDOW) must be available; the logret is then gated to its END minute
    in the left-open ``(T−_RV_WINDOW, T]`` (so the bar at exactly T−_RV_WINDOW supplies a prior close but its
    own out-of-window logret is excluded). ``present`` is the value-truth's per-T row gate (the rolling form
    emits an RV row at minute T only for a symbol with a bar at T)."""
    prev_close = close[:, :-1]
    cur_close = close[:, 1:]
    prev_min = minute[:, :-1]
    cur_min = minute[:, 1:]
    exact_step = (prev_min >= 0) & (cur_min >= 0) & ((cur_min - prev_min) == 60)
    # the logret's END minute must be inside the left-open RV window (T−_RV_WINDOW, T].
    in_window = (cur_min > now_epoch - _RV_WINDOW * 60) & (cur_min <= now_epoch)
    valid = exact_step & in_window & (prev_close > 0) & (cur_close > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        logret = np.where(valid, np.log(cur_close / prev_close), np.nan)
    mask = np.isfinite(logret)
    n = mask.sum(axis=1).astype(np.float64)
    x = np.where(mask, logret, 0.0)
    sum_x = x.sum(axis=1)
    sum_x2 = (x * x).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (sum_x2 - sum_x * sum_x / n) / (n - 1.0)
        rv = np.sqrt(np.clip(var, 0.0, None))
    rv = np.where(n >= _RV_MIN_OBS, rv, np.nan)
    return np.where(present, rv, np.nan)


def _universe_mean(values: np.ndarray) -> float:
    """The universe equal-weight mean over the FINITE entries (nulls auto-excluded, the legacy polars ``mean``);
    NaN where no symbol has a valid measurement that minute."""
    finite = np.isfinite(values)
    if not finite.any():
        return np.nan
    return float(values[finite].mean())


class MarketTurbulenceClean:
    """CROSS_SECTIONAL gather: universe-wide realized-move-magnitude SCALARS broadcast to every present symbol —
    NOT a per-symbol fold (same per-minute universe reduce as breadth/return_dispersion). Per minute T:

      * ``mkt_absret_{W}m`` = universe equal-weight mean of ``|close[T]/close[T−W] − 1|`` (the realized
        whole-market MOVE MAGNITUDE over the trailing W minutes — turbulence, not direction), and
      * ``mkt_rv_30m``      = universe equal-weight mean of each symbol's trailing-30m realized vol (std of 1m
        log returns over (T−30, T], ≥10 obs) — the universe-mean realized-vol level.

    The denominator is symbols with a VALID measurement that minute (a close at BOTH T and T−W for absret; ≥10
    valid 1m logrets in (T−30, T] for RV), computed identically and present()-gated at T (the value-truth /
    rolling BATCH form emits a measure row at minute T only for a symbol with a bar at T). Legacy:
    ``MarketTurbulenceGroup``; one of the live REDUCE_GROUPS."""

    name = "market_turbulence"
    input_cols = ("close",)
    _ABSRET_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
    feature_names = tuple(f"mkt_absret_{w}m" for w in _ABSRET_WINDOWS) + (f"mkt_rv_{_RV_WINDOW}m",)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        minute = window.trailing_minute()
        present = window.present()
        latest_close = window.latest("close")
        now_epoch = window.minute_epoch
        out: dict[str, np.ndarray] = {}
        for w in self._ABSRET_WINDOWS:
            lag_close = _value_at_lag(close, minute, now_epoch, w)
            with np.errstate(invalid="ignore", divide="ignore"):
                absret = np.abs(latest_close / lag_close - 1.0)
            # present()-gate at T: a symbol absent at T has no close[T] (its latest_close is carried/stale), so
            # it contributes no |return| to the universe mean — the value-truth's per-T measurement set.
            absret = np.where(present, absret, np.nan)
            mkt = _universe_mean(absret)
            out[f"mkt_absret_{w}m"] = np.where(present, mkt, np.nan)
        # RV: per-symbol std of 1m logrets over (T−RV_WINDOW, T] — read over the FULL trailing close (the
        # logret at the window's left edge needs the close one step before it), gated to its end-minute inside.
        rv = _market_realized_vol(close, minute, present, now_epoch)
        mkt_rv = _universe_mean(rv)
        out[f"mkt_rv_{_RV_WINDOW}m"] = np.where(present, mkt_rv, np.nan)
        return out


class SectorReturnClean:
    """CROSS_SECTIONAL: per window — sector_return_{w}m = the equal-weight mean trailing-w return of the
    ticker's GICS sector (present members), sector_excess_{w}m = the ticker's own trailing-w return minus that
    sector mean. Unmapped-sector names → NULL; absent symbols → NULL (sparse). present()-gated (an absent
    member is not in the sector mean). Legacy: ``SectorReturnGroup``. Reads ``window.static['sector']``."""

    name = "sector_return"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
    _UNKNOWN = -1
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("sector_return", "sector_excess")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        present = window.present()
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        sector = window.static.get("sector")
        if sector is None:
            sector = np.full(n_sym, self._UNKNOWN)
        # a row is emitted only for a present, mapped symbol (sparse policy + unmapped→NULL).
        keep = present & (sector != self._UNKNOWN)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(n_sym, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                own_ret = latest_close / prior - 1.0
            sec_mean = _sector_mean_vector(own_ret, sector, present)
            out[f"sector_return_{w}m"] = np.where(keep, sec_mean, np.nan)
            out[f"sector_excess_{w}m"] = np.where(keep, own_ret - sec_mean, np.nan)
        return out


class PeerRelativeClean:
    """CROSS_SECTIONAL: peer_relative_ret_{w}m = the symbol's trailing-w return minus the equal-weight mean
    trailing-w return of its BEHAVIORAL-PEER cluster (the SVD co-movement cluster) at that minute — the
    idiosyncratic move not explained by its co-movement group. Same shape as sector_excess but grouped by
    ``window.static['cluster_id']``. NULL cluster → NULL; absent symbol → NULL (sparse). present()-gated.
    Legacy: ``PeerRelativeGroup``."""

    name = "peer_relative"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 15, 30)
    _UNKNOWN = -1
    feature_names = tuple(f"peer_relative_ret_{w}m" for w in _WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        present = window.present()
        latest_close = window.latest("close")
        n_sym = close.shape[0]
        cluster = window.static.get("cluster_id")
        if cluster is None:
            cluster = np.full(n_sym, self._UNKNOWN)
        keep = present & (cluster != self._UNKNOWN)
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            if close.shape[1] > w:
                prior = close[:, -(w + 1)]
            else:
                prior = np.full(n_sym, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                own_ret = latest_close / prior - 1.0
            peer_mean = _sector_mean_vector(own_ret, cluster, present)
            out[f"peer_relative_ret_{w}m"] = np.where(keep, own_ret - peer_mean, np.nan)
        return out


class SectorBetaClean:
    """CROSS_SECTIONAL: per window, the rolling OLS of each name's one-minute return on its OWN GICS sector's
    equal-weight one-minute return — sector_beta_{w} (slope) + sector_corr_{w} (corr in [-1,1]). The sector
    aggregate is a per-(minute,sector) present-symbol equal-weight mean. Unmapped-sector names → NULL. Legacy:
    ``SectorBetaGroup``. Reads ``window.static['sector']`` for the per-symbol GICS label."""

    name = "sector_beta"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (15, 30, 60)
    _UNKNOWN = -1  # the unmapped-sector sentinel
    feature_names = tuple(f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("sector_beta", "sector_corr"))

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        n_sym = close.shape[0]
        sector = window.static.get("sector")
        if sector is None:
            sector = np.full(n_sym, self._UNKNOWN)
        own_ret = _buffer_returns(close)
        sec_ret = _sector_equal_weight_returns(own_ret, sector)
        is_unknown = sector == self._UNKNOWN
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy rolling_sum_by('minute','Wm') over the OLS): keep only the paired returns
            # whose minute is within the last W minutes (mask from the close time-window), the rest NaN.
            in_window = np.isfinite(window.trailing_time("close", w))
            yw = np.where(in_window, own_ret, np.nan)  # y = own return
            xw = np.where(in_window, sec_ret, np.nan)  # x = sector return
            beta, corr = _windowed_sector_ols(yw, xw)
            # unmapped-sector names → NULL (no sector series to regress on)
            out[f"sector_beta_{w}m"] = np.where(is_unknown, np.nan, beta)
            out[f"sector_corr_{w}m"] = np.where(is_unknown, np.nan, corr)
        return out


def _align_market_return(minute: np.ndarray, spy_ret: np.ndarray, spy_minute: np.ndarray) -> np.ndarray:
    """Map SPY's per-bar return onto every (symbol, bar) cell BY MINUTE — for each symbol's bar at minute m,
    the SPY return at the SAME minute m (NaN where SPY has no bar at m). This is the legacy minute-join: each
    symbol's ring column is its OWN present minute, so a positional broadcast of SPY's row would pair a sparse
    name's bar with SPY's bar at a DIFFERENT minute. Vectorized via searchsorted on SPY's (monotonic) valid
    minutes."""
    valid = (spy_minute >= 0) & np.isfinite(spy_ret)
    vmin = spy_minute[valid]
    vret = spy_ret[valid]
    out = np.full(minute.shape, np.nan)
    if vmin.size == 0:
        return out
    order = np.argsort(vmin, kind="stable")  # SPY minutes ascending (the buffer is oldest→newest already)
    vmin_sorted = vmin[order]
    vret_sorted = vret[order]
    flat = minute.reshape(-1)
    pos = np.searchsorted(vmin_sorted, flat)
    in_range = (pos < vmin_sorted.size) & (flat >= 0)
    pos_clipped = np.clip(pos, 0, vmin_sorted.size - 1)
    hit = in_range & (vmin_sorted[pos_clipped] == flat)  # an exact-minute match in SPY
    aligned = np.where(hit, vret_sorted[pos_clipped], np.nan)
    return aligned.reshape(minute.shape)


_MARKET_BETA_MAX = 15.0  # |beta| above this → NULL (legacy BETA_MAX — a degenerate near-zero-var-SPY fit)


class MarketBetaClean:
    """CROSS_SECTIONAL: per window, the OLS of each name's one-minute return (y) on SPY's one-minute return (x,
    the same broadcast value for every symbol) — a VALUE-OLS, NOT a time-OLS (x is SPY's return, not the minute,
    so NO rebased-minute axis). market_beta_{w}m = slope (NULL when |beta|>15), market_corr_{w}m = corr in
    [-1,1], idio_vol_{w}m = std(own ret)·sqrt(clip(1−r2,0,1)). TIME-windowed (legacy std_/OLS = rolling_*_by).
    SPY's row index from ``window.static['spy_row']``. Legacy: ``MarketBetaGroup`` (ReductionGroup)."""

    name = "market_beta"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (10, 15, 30, 45, 60, 90, 120)
    feature_names = tuple(
        f"{prefix}_{w}m" for w in _WINDOWS for prefix in ("market_beta", "market_corr", "idio_vol")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        n_sym = close.shape[0]
        spy_row = window.static.get("spy_row")
        if spy_row is None:
            nan = np.full(n_sym, np.nan)
            return {name: nan for name in self.feature_names}
        spy_idx = int(np.asarray(spy_row).flat[0])
        own_ret = _buffer_returns(close)  # (n_sym, buffer) per-bar 1m return
        minute = window.trailing_minute()  # (n_sym, buffer) the minute-epoch at each bar (-1 empty)
        # SPY's return must be paired to each symbol BY MINUTE, not by buffer position — each symbol's ring
        # column is its OWN present minute, so SPY's column t is a different minute than a sparse name's column
        # t. Build a minute→SPY-return lookup from SPY's own (minute, return) bars, then map every symbol's
        # minute matrix through it (NaN where SPY has no bar at that minute). This is the legacy minute-join.
        mkt_ret = _align_market_return(minute, own_ret[spy_idx], minute[spy_idx])
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            # TIME window (legacy std_/OLS = rolling_*_by("minute")): mask the paired returns to the last w mins.
            in_window = np.isfinite(window.trailing_time("close", w))
            x = np.where(in_window, mkt_ret, np.nan)  # x = SPY return (the regressor)
            y = np.where(in_window, own_ret, np.nan)  # y = own return (the regressand)
            beta = _row_ols_slope(x, y)
            corr = _row_corr(x, y)
            own_std = _row_std(y)
            with np.errstate(invalid="ignore"):
                r2 = corr * corr
                idio = own_std * np.sqrt(np.clip(1.0 - np.clip(r2, 0.0, 1.0), 0.0, None))
            out[f"market_beta_{w}m"] = np.where(np.abs(beta) <= _MARKET_BETA_MAX, beta, np.nan)
            out[f"market_corr_{w}m"] = corr
            out[f"idio_vol_{w}m"] = idio
        return out


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
