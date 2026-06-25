"""Daily-snapshot feature groups ported to the ``CleanEngine`` interface, + the shared daily-data plumbing.

A daily-snapshot group is intraday-INVARIANT: its features are computed ONCE per session from the settled
DAILY bars (prior-day closes, multi-day returns, N-day highs, ADV) and broadcast to every minute of the day.
The engine carries the daily history in the per-session memo (``window.session``), populated once at the
session boundary via ``CleanEngine.set_session`` — the daily analogue of the minute trailing buffer.

SESSION SCHEMA (the shared plumbing — built once, read by every daily-snapshot group):
  ``session["daily_close"]`` : ``(n_symbols, n_days)`` matrix of daily closes, newest column LAST. CONVENTION
      (Lead ruling, [-1]=TODAY): the newest column ``[:, -1]`` is TODAY's (partial/current) daily bar — the
      faithful image of legacy, whose ``daily`` source frame includes today and each group does its own
      ``shift(1)`` for the prior completed day. So the prior COMPLETED day D-1 = ``[:, -2]`` (the ``_asof``
      anchor a point-in-time daily feature reads), and a w-completed-day reference = ``[:, -(w+2)]``. The
      ``_completed(...)`` helper (``[:, :-1]``) is the today-excluded series for the multi-day vol / high
      windows. (prior_day's gap_open / overnight_split's today reads use ``[:, -1]`` directly — that's why
      today MUST be present.) NaN-padded on the left where a symbol has fewer days.
  ``session["daily_high"]``  : ``(n_symbols, n_days)`` daily highs (for N-day-high distances), same convention.
  ``session["daily_open"]``  : ``(n_symbols, n_days)`` daily opens (today's open = ``[:, -1]`` for gap_open).
  ``session["daily_volume"]``: ``(n_symbols, n_days)`` daily volumes (for ADV / dollar-volume).
  ``session["daily_vwap"]``  : ``(n_symbols, n_days)`` daily volume-weighted average prices (for multi-day VWAP),
      same convention. Only ``multi_day_vwap`` reads it; absent → that group emits NaN.
A group derives its features from these matrices exactly as a windowed group derives from the trailing buffer.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window
from quantlib.features.clean_groups_xsectional import _average_rank

_ADV_WINDOW = 20
_ADV_MIN_DAYS = 10


def _daily_window(mat: np.ndarray, w: int) -> np.ndarray:
    """The last ``w`` columns of a ``(n_symbols, n_days)`` daily matrix."""
    return mat[:, -w:]


def _completed(daily_close: np.ndarray) -> np.ndarray:
    """The settled daily-close matrix EXCLUDING today's (partial/current) bar — the prior-completed-day series
    the point-in-time daily features read. Under the session convention newest col [:, -1] = TODAY (the partial
    bar), so the completed history is everything up to and INCLUDING D-1 = ``daily_close[:, :-1]`` (its own
    newest col is then D-1). A group's _asof = D-1 = ``_completed(...)[:, -1]`` = ``daily_close[:, -2]``."""
    return daily_close[:, :-1]


def _asof(daily_close: np.ndarray) -> np.ndarray:
    """The prior COMPLETED day's close (D-1) — the point-in-time daily anchor (legacy ``close.shift(1)``). Under
    [-1]=today this is ``daily_close[:, -2]`` (the col before today). NaN where the symbol has <2 daily bars.
    """
    if daily_close.shape[1] < 2:
        return np.full(daily_close.shape[0], np.nan)
    return daily_close[:, -2]


def _daily_return(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Return over the last ``w`` COMPLETED days: ``_asof / close[D-1-w] − 1`` (legacy _asof/_asof.shift(w),
    anchored at D-1 = [:, -2], so close[D-1-w] = [:, -(w+2)]). NaN where the symbol lacks w+2 daily bars."""
    n_sym, n_days = daily_close.shape
    asof = _asof(daily_close)
    if n_days > w + 1:
        ref = daily_close[:, -(w + 2)]
    else:
        ref = np.full(n_sym, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return asof / ref - 1.0


def _daily_vol(daily_close: np.ndarray, w: int) -> np.ndarray:
    """Std (ddof=1) of the last ``w`` COMPLETED-day returns ending at the prior close (D-1). Computed over the
    completed series only (today's partial bar excluded — it would splice an incomplete (today/D-1) return).
    """
    completed = _completed(daily_close)  # exclude today's partial bar
    with np.errstate(invalid="ignore", divide="ignore"):
        rets = completed[:, 1:] / completed[:, :-1] - 1.0  # (n_sym, n_completed-1)
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
    """Prior close (D-1) relative to its trailing ``w``-COMPLETED-day high: ``_asof / max(close over w completed
    days) − 1`` (≤ 0). The high window is over the completed series (today's partial bar excluded). NULL until
    the full ``w``-day window is present (legacy rolling_max min_periods=w — a short history can't form the
    w-day high)."""
    completed = _completed(daily_close)
    asof = _asof(daily_close)
    win = _daily_window(completed, w)  # completed days only (today excluded)
    n_present = np.isfinite(win).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        high = np.nanmax(np.where(np.isfinite(win), win, -np.inf), axis=1)
        dist = asof / high - 1.0
    # require the FULL w-day completed window (legacy rolling_max NULLs a short window).
    return np.where(n_present >= w, dist, np.nan)


class MultiDayClean:
    """DAILY-SNAPSHOT: multi-day return / volatility / N-day-high distance from the settled daily closes,
    point-in-time as of the prior close. daily_return_{w}d, daily_vol_{w}d, dist_from_{w}d_high. Reads
    ``window.session['daily_close']``. Legacy: ``MultiDayReturnGroup`` (name "multi_day_returns")."""

    name = "multi_day_returns"
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


_VWAP_DAYS: tuple[int, ...] = (5, 10, 20, 60, 120)  # ~week, 2wk, month, quarter, half-year


def _ndays_vwap(daily_vwap: np.ndarray, daily_volume: np.ndarray, w: int) -> np.ndarray:
    """The ``w``-day volume-weighted average price ending at the prior COMPLETED day (D-1):
    ``sum(vwap·volume over the last w completed days) / sum(volume over the same)``. Legacy shifts pv/vol by 1
    (prior completed day) then ``rolling_sum(window=w)`` whose default ``min_periods==w`` NULLs a short or
    gappy window — so the sum requires ALL ``w`` completed days finite (a NaN anywhere → NaN, like polars'
    null-in-window propagation). Computed over the today-excluded series ``[:, :-1]``."""
    pv = _completed(daily_vwap) * _completed(daily_volume)  # (n_sym, n_completed)
    vol = _completed(daily_volume)
    pv_win = _daily_window(pv, w)
    vol_win = _daily_window(vol, w)
    full = (np.isfinite(pv_win).sum(axis=1) >= w) & (np.isfinite(vol_win).sum(axis=1) >= w)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap_n = pv_win.sum(axis=1) / vol_win.sum(axis=1)
    return np.where(full, vwap_n, np.nan)


class MultiDayVwapClean:
    """DAILY-SNAPSHOT: where the prior close sits versus the volume-weighted average price over the last
    week / 2wk / month / quarter / half-year. dist_from_vwap_{w}d = _pc/vwap_{w}d − 1; above_vwap_{w}d =
    (_pc > vwap_{w}d). The N-day VWAP is point-in-time as of the prior close (never today's incomplete bar).
    Reads ``window.session['daily_vwap'/'daily_volume'/'daily_close']``. Legacy: ``MultiDayVwapGroup``."""

    name = "multi_day_vwap"
    input_cols = ()
    feature_names = tuple(
        col for days in _VWAP_DAYS for col in (f"dist_from_vwap_{days}d", f"above_vwap_{days}d")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        daily_close = window.session.get("daily_close")
        daily_volume = window.session.get("daily_volume")
        daily_vwap = window.session.get("daily_vwap")
        if daily_close is None or daily_volume is None or daily_vwap is None:
            return {name: np.full(n, np.nan) for name in self.feature_names}
        prior_close = _asof(daily_close)  # _pc = close.shift(1) = D-1
        out: dict[str, np.ndarray] = {}
        for days in _VWAP_DAYS:
            vwap_n = _ndays_vwap(daily_vwap, daily_volume, days)
            with np.errstate(invalid="ignore", divide="ignore"):
                dist = prior_close / vwap_n - 1.0
            out[f"dist_from_vwap_{days}d"] = dist
            above = np.where(np.isfinite(dist), (prior_close > vwap_n).astype(np.float64), np.nan)
            out[f"above_vwap_{days}d"] = above
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


def _leg_beta(name_rets: np.ndarray, mkt_rets: np.ndarray, w: int, min_pairs: int) -> np.ndarray:
    """Rolling-``w``-day OLS beta of ``name_rets`` (n_sym, n) on the 1-D market leg ``mkt_rets`` (n,):
    ``cov(name, mkt) / var(mkt)`` over the trailing ``w`` days where BOTH are finite. NaN where <``min_pairs``
    finite pairs or the market-leg variance is 0 (beta undefined). ddof matches polars rolling_var/cov
    (sample, divide by n−1)."""
    name_w = name_rets[:, -w:]
    mkt_w = np.broadcast_to(mkt_rets[-w:], name_w.shape)
    mask = np.isfinite(name_w) & np.isfinite(mkt_w)
    npairs = mask.sum(axis=1).astype(np.float64)
    x = np.where(mask, mkt_w, 0.0)  # market (regressor)
    y = np.where(mask, name_w, 0.0)  # name (regressand)
    sx, sy = x.sum(axis=1), y.sum(axis=1)
    sxx, sxy = (x * x).sum(axis=1), (x * y).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = (sxy - sx * sy / npairs) / (npairs - 1.0)
        var_x = (sxx - sx * sx / npairs) / (npairs - 1.0)
        beta = cov / var_x
    defined = (npairs >= min_pairs) & (var_x > 0.0)
    return np.where(defined, beta, np.nan)


class OvernightBetaClean:
    """DAILY-SNAPSHOT: the W11 overnight/intraday beta asymmetry. overnight_beta_60d = rolling-60d OLS beta of
    the name's OVERNIGHT return (open/prev_close − 1) on SPY's overnight return; intraday_beta_60d = same for
    the INTRADAY return (close/open − 1); beta_overnight_minus_intraday = the difference (the W11 asymmetry).
    NaN if <20 finite pairs or the SPY-leg variance is 0. Reads ``window.session['daily_open'/'daily_close']``
    + the SPY row from ``window.static['spy_row']``. Legacy: ``OvernightBetaGroup``."""

    name = "overnight_beta"
    input_cols = ()
    feature_names = ("overnight_beta_60d", "intraday_beta_60d", "beta_overnight_minus_intraday")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        nan = np.full(n, np.nan)
        daily_open = window.session.get("daily_open")
        daily_close = window.session.get("daily_close")
        spy_row = window.static.get("spy_row")
        if daily_open is None or daily_close is None or spy_row is None or daily_close.shape[1] < 2:
            return {name: nan for name in self.feature_names}
        spy_idx = int(np.asarray(spy_row).flat[0])
        # Build both legs over the FULL daily series (one return per day, like legacy's per-row columns): the
        # OVERNIGHT leg needs the prior close so day 0 is NaN (no prev_close); the INTRADAY leg (close/open) is
        # valid on day 0 too. Keeping day 0 in the intraday leg matters near warmup — it is one extra finite
        # pair the overnight leg lacks, so the two legs legitimately regress over different sample counts.
        with np.errstate(invalid="ignore", divide="ignore"):
            on_ret = np.full_like(daily_close, np.nan)
            on_ret[:, 1:] = (
                daily_open[:, 1:] / daily_close[:, :-1] - 1.0
            )  # open / prev_close − 1 (day 0 = NaN)
            id_ret = daily_close / daily_open - 1.0  # close / open − 1 (valid every day, incl day 0)
        overnight = _leg_beta(on_ret, on_ret[spy_idx], _DAILY_BETA_WINDOW, _DAILY_BETA_MIN_PAIRS)
        intraday = _leg_beta(id_ret, id_ret[spy_idx], _DAILY_BETA_WINDOW, _DAILY_BETA_MIN_PAIRS)
        return {
            "overnight_beta_60d": overnight,
            "intraday_beta_60d": intraday,
            "beta_overnight_minus_intraday": overnight - intraday,
        }


class OvernightIntradaySplitClean:
    """DAILY-SNAPSHOT: the overnight/intraday return split of the latest daily bar (broadcast to every minute).
    intraday_ret = close/open − 1; overnight_minus_intraday = (open/prev_close − 1) − intraday_ret;
    overnight_share = |overnight| / (|overnight| + |intraday|), NULL when the total move is 0. Reads
    ``window.session['daily_open'/'daily_close']``. Legacy: ``OvernightIntradaySplitGroup``."""

    name = "overnight_intraday_split"
    input_cols = ()
    feature_names = ("intraday_ret", "overnight_minus_intraday", "overnight_share")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        nan = np.full(n, np.nan)
        daily_open = window.session.get("daily_open")
        daily_close = window.session.get("daily_close")
        if daily_open is None or daily_close is None or daily_close.shape[1] < 2:
            return {name: nan for name in self.feature_names}
        op = daily_open[:, -1]  # latest daily open
        close = daily_close[:, -1]  # latest daily close
        prev_close = daily_close[:, -2]  # the prior day's close
        with np.errstate(invalid="ignore", divide="ignore"):
            overnight = op / prev_close - 1.0
            intraday = close / op - 1.0
            abs_total = np.abs(overnight) + np.abs(intraday)
            overnight_share = np.where(abs_total > 0.0, np.abs(overnight) / abs_total, np.nan)
        return {
            "intraday_ret": intraday,
            "overnight_minus_intraday": overnight - intraday,
            "overnight_share": overnight_share,
        }


class LiquidityRankClean:
    """DAILY-SNAPSHOT: the slow persistent liquidity TIER. adv_dollar_log_20d = log1p of the trailing-20-day
    mean dollar volume (close·volume), min 10 days; liquidity_rank = the symbol's cross-sectional PERCENTILE
    (rank(method='average')/count, 1=most liquid) of that ADV within the day's universe. Reads
    ``window.session['daily_close'/'daily_volume']``. Legacy: ``LiquidityRankGroup`` (DailySnapshotGroup)."""

    name = "liquidity_rank"
    input_cols = ()
    feature_names = ("adv_dollar_log_20d", "liquidity_rank")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        daily_close = window.session.get("daily_close")
        daily_volume = window.session.get("daily_volume")
        if daily_close is None or daily_volume is None:
            return {name: np.full(n, np.nan) for name in self.feature_names}
        dvol = daily_close * daily_volume  # (n_sym, n_days)
        win = _daily_window(dvol, _ADV_WINDOW)
        mask = np.isfinite(win)
        n_days = mask.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            adv = np.where(n_days >= _ADV_MIN_DAYS, np.where(mask, win, 0.0).sum(axis=1) / n_days, np.nan)
            adv_log = np.log1p(adv)
        # cross-sectional percentile of adv over the universe (symbols with a valid adv): rank/count.
        ranks = _average_rank(adv)
        count = np.isfinite(adv).sum()
        with np.errstate(invalid="ignore", divide="ignore"):
            liquidity_rank = ranks / count if count > 0 else np.full(n, np.nan)
        return {"adv_dollar_log_20d": adv_log, "liquidity_rank": liquidity_rank}
