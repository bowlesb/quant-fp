"""Worked examples: feature groups ported to the one ``CleanEngine`` interface.

These show the migration shape — a group's math becomes ONE small numpy ``compute(window)`` over the carried
buffer, replacing its legacy ``reduced()/regressions()/assemble()`` declarative form (or its bespoke polars
``compute()``). The arithmetic is the same; it is expressed once, framework-free, and the live step and the
backfill are the same replay through the engine. (The full migration ports all ~68 groups this way; these four
prove the interface covers the diverse shapes: a rolling OLS, a windowed volume-weighted ratio, a windowed mean
of a per-bar derived quantity, and per-bar candlestick geometry + a two-candle lag-1 pattern.)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from quantlib.features.clean_engine import Window
from quantlib.features.clean_groups_stateful import _et_session
from quantlib.features.clean_groups_windowed import (
    _close_at_lag,
    _denom_x_defined,
    _rebased_minute_axis,
    _row_mean,
    _row_sum,
)

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)


def _trailing_window(mat: np.ndarray, w: int) -> np.ndarray:
    """The last ``w`` columns of the (n_symbols, window) trailing matrix — each symbol's most recent ``w``
    present bars (NaN-padded on the left where it has fewer)."""
    return mat[:, -w:]


class TrendQualityClean:
    """Trailing OLS of close on time over each window: normalized slope + R² + signed strength. Legacy:
    ``TrendQualityGroup`` (ReductionGroup).

    TIME-windowed (legacy ``rolling_*_by("minute")``): the OLS is over the bars whose minute is in the last
    ``w`` minutes (``trailing_time``), NOT the last ``w`` positional slots — sparse symbols diverge otherwise.
    The time regressor x is the ACTUAL bar minute rebased to the earliest present minute (a ``kind="time"``
    regression, ``_rebased_minute_axis``), NOT a positional arange. price_slope = slope/mean(close over w);
    price_r2 = cov²/(var_x·var_y) with the legacy denom floors; FLAT-price-over-a-warmed-window (slope defined,
    var_y==0) pins R²=0 (a real zero-explained-variance fit), NOT NaN — matching legacy assemble()."""

    name = "trend_quality"
    input_cols = ("close",)
    _WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)  # legacy TrendQualityGroup.WINDOWS
    feature_names = tuple(
        f"{stat}_{w}m" for w in _WINDOWS for stat in ("price_slope", "price_r2", "trend_strength")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")  # (n_symbols, window), oldest→newest
        time_axis = _rebased_minute_axis(window.trailing_minute())
        out: dict[str, np.ndarray] = {}
        for w in self._WINDOWS:
            in_window = np.isfinite(window.trailing_time("close", w))
            y = np.where(in_window, close, np.nan)
            x = np.where(in_window, time_axis, np.nan)
            mask = np.isfinite(x) & np.isfinite(y)
            nb = mask.sum(axis=1).astype(np.float64)
            xf = np.where(mask, x, 0.0)
            yf = np.where(mask, y, 0.0)
            sx, sy = xf.sum(axis=1), yf.sum(axis=1)
            sxx, syy, sxy = (xf * xf).sum(axis=1), (yf * yf).sum(axis=1), (xf * yf).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                cov = nb * sxy - sx * sy
                var_x = nb * sxx - sx * sx
                var_y = nb * syy - sy * sy
                slope = cov / var_x
                mean_y = sy / nb
                norm_slope = slope / mean_y  # fractional move per minute
                r2 = (cov * cov) / (var_x * var_y)
            # slope well-posedness = the legacy X-side floors (n≥2 AND both denom_x floors), not a bare var_x>0.
            slope_defined = _denom_x_defined(nb, sx, sxx, var_x)
            norm_slope = np.where(slope_defined, norm_slope, np.nan)
            # R²: defined where var_y>0; FLAT price (slope defined but var_y==0) → R²=0 (legacy assemble pin),
            # not NaN — a flat line has zero EXPLAINED variance, not an undefined fit.
            r2 = np.where(slope_defined & (var_y > 0.0), np.clip(r2, 0.0, 1.0), np.nan)
            r2 = np.where(slope_defined & ~(var_y > 0.0), 0.0, r2)
            out[f"price_slope_{w}m"] = norm_slope
            out[f"price_r2_{w}m"] = r2
            out[f"trend_strength_{w}m"] = norm_slope * r2  # signed quality-weighted strength
        return out


class VwapDeviationClean:
    """Close relative to its trailing volume-weighted average price over each window: close/vwap − 1.
    The ratio half of ``price_volume``, as one numpy function over the carried close+volume windows."""

    name = "vwap_deviation"
    input_cols = ("close", "volume")
    feature_names = tuple(f"vwap_deviation_{w}m" for w in WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        volume = window.trailing("volume")
        latest_close = window.latest("close")
        cv = close * volume
        out: dict[str, np.ndarray] = {}
        for w in WINDOWS:
            # TIME window (legacy rolling_sum_by("minute")): the VWAP sums are over the bars in the last w
            # minutes, NOT the last w positional slots — a sparse symbol diverges otherwise.
            in_window = np.isfinite(window.trailing_time("close", w))
            vol = _row_sum(np.where(in_window, volume, np.nan))
            cv_w = _row_sum(np.where(in_window, cv, np.nan))
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = cv_w / vol
                dev = latest_close / vwap - 1.0
            out[f"vwap_deviation_{w}m"] = np.where(vol > 0, dev, np.nan)
        return out


REALIZED_WINDOWS: tuple[int, ...] = (3, 5, 10)


class RealizedRangeClean:
    """Trailing mean of the intra-minute high-low range as a fraction of close ((high-low)/close), over short
    windows — ``realized_range``'s ``rv3``. A windowed mean of a per-bar derived quantity; one numpy function.
    """

    name = "realized_range"
    input_cols = ("high", "low", "close")
    feature_names = tuple(f"realized_range_{w}m" for w in REALIZED_WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        high = window.trailing("high")
        low = window.trailing("low")
        close = window.trailing("close")
        with np.errstate(invalid="ignore", divide="ignore"):
            rng = (high - low) / close  # per-bar range fraction, (n, window)
        out: dict[str, np.ndarray] = {}
        for w in REALIZED_WINDOWS:
            # TIME window (legacy rolling_mean_by("minute")): the mean is over the bars in the last w minutes,
            # NOT the last w positional slots — a sparse symbol diverges otherwise.
            in_window = np.isfinite(window.trailing_time("close", w))
            mean_rng, _ = _row_mean(np.where(in_window, rng, np.nan))
            out[f"realized_range_{w}m"] = mean_rng
        return out


_TRUE: np.ndarray = np.array(True)  # sentinel: a current-bar operand whose value is always KNOWN (broadcast)


def _kleene_and(operands: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    """Fold a boolean AND chain in polars' KLEENE 3-valued logic, per (value, is_known) operand. Result float:
    0.0 if ANY operand is definitely False (value False AND known), NaN if no operand is False but ≥1 is unknown
    (a null prev-bar term), else 1.0. ``is_known`` may be ``_TRUE`` (the operand is always known — a current-bar
    term) or a per-symbol bool mask (known iff the prior bar exists). Matches the legacy ``(a & b & ...).cast``
    where a null operand makes the chain null unless an earlier operand already forced it False."""
    n = operands[0][0].shape[0]
    any_false = np.zeros(n, dtype=bool)
    any_unknown = np.zeros(n, dtype=bool)
    for value, known in operands:
        is_known = np.broadcast_to(known, value.shape)
        any_false |= is_known & ~value  # a known-False operand → the AND is definitely False
        any_unknown |= ~is_known  # an unknown operand → the AND may be null
    out = np.ones(n, dtype=np.float64)
    out[any_unknown & ~any_false] = np.nan  # unknown wins only when nothing is already False
    out[any_false] = 0.0
    return out


class CandlestickClean:
    """CANDLESTICK: 8 single-bar geometry features (per-bar arithmetic on the latest OHLC) + 4 two-candle
    patterns (engulfing/harami) that read the PRIOR bar. Legacy: ``CandlestickGroup`` (StatefulGroup).

    The two-candle patterns read ``_prev_open``/``_prev_close`` via legacy ``LagSpec(minutes=1)`` = a STRICT
    TIME-lag (the bar at EXACTLY T−1 minute, NULL when that minute is absent — == base.lagged), NOT the
    immediately-prior PRESENT bar. On a gap the patterns are NULL (nan_policy=warmup), not computed off a stale
    bar — so they use ``_close_at_lag`` (the strict T−1 lookup), not ``trailing[:, -2]``. The 8 single-bar
    features are pure per-bar geometry on the latest OHLC (genuinely positional / point-in-time)."""

    name = "candlestick"
    input_cols = ("open", "high", "low", "close")
    feature_names = (
        "body_ratio",
        "upper_shadow_ratio",
        "lower_shadow_ratio",
        "is_bullish",
        "is_doji",
        "is_hammer",
        "is_shooting_star",
        "is_marubozu",
        "pattern_engulfing_bullish",
        "pattern_engulfing_bearish",
        "pattern_harami_bullish",
        "pattern_harami_bearish",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        o = window.latest("open")
        h = window.latest("high")
        low = window.latest("low")
        c = window.latest("close")
        rng = h - low
        positive = rng > 0.0
        body_top = np.maximum(o, c)
        body_bottom = np.minimum(o, c)
        with np.errstate(invalid="ignore", divide="ignore"):
            body = np.where(positive, np.abs(c - o) / rng, 0.0)
            upper = np.where(positive, (h - body_top) / rng, 0.0)
            lower = np.where(positive, (body_bottom - low) / rng, 0.0)
        curr_bull = c > o
        curr_bear = c < o
        # prior bar's open/close at EXACTLY T−1 minute (strict time-lag, NaN on a gap).
        minute = window.trailing_minute()
        now_epoch = window.minute_epoch
        prev_open = _close_at_lag(window.trailing("open"), minute, now_epoch, 1)
        prev_close = _close_at_lag(window.trailing("close"), minute, now_epoch, 1)
        prev_known = np.isfinite(prev_open) & np.isfinite(prev_close)
        out: dict[str, np.ndarray] = {
            "body_ratio": body,
            "upper_shadow_ratio": upper,
            "lower_shadow_ratio": lower,
            "is_bullish": curr_bull.astype(np.float64),
            "is_doji": (body < 0.1).astype(np.float64),
            "is_hammer": ((lower > 0.6) & (upper < 0.1) & (body < 0.4)).astype(np.float64),
            "is_shooting_star": ((upper > 0.6) & (lower < 0.1) & (body < 0.4)).astype(np.float64),
            "is_marubozu": (body > 0.9).astype(np.float64),
        }
        # two-candle patterns: a chain of boolean ANDs where every PREV-dependent term is UNKNOWN (null) when
        # the T−1 bar is absent, while the current-bar terms (curr_bull/curr_bear) are always known. polars
        # evaluates the chain in KLEENE 3-valued logic → 0.0 if ANY operand is definitely False (e.g. curr_bear
        # already fails), null ONLY if no operand is False but ≥1 is unknown. A blanket "prev absent → NaN" is
        # WRONG (legacy emits 0.0 when a current-bar term fails). _kleene_and folds (value, is_known) operands.
        with np.errstate(invalid="ignore"):
            out["pattern_engulfing_bullish"] = _kleene_and(
                [
                    (curr_bull, _TRUE),
                    (prev_close < prev_open, prev_known),
                    (c >= prev_open, prev_known),
                    (o <= prev_close, prev_known),
                ]
            )
            out["pattern_engulfing_bearish"] = _kleene_and(
                [
                    (curr_bear, _TRUE),
                    (prev_close > prev_open, prev_known),
                    (o >= prev_close, prev_known),
                    (c <= prev_open, prev_known),
                ]
            )
            out["pattern_harami_bullish"] = _kleene_and(
                [
                    (curr_bull, _TRUE),
                    (prev_close < prev_open, prev_known),
                    (body_top <= prev_open, prev_known),
                    (body_bottom >= prev_close, prev_known),
                ]
            )
            out["pattern_harami_bearish"] = _kleene_and(
                [
                    (curr_bear, _TRUE),
                    (prev_close > prev_open, prev_known),
                    (body_top <= prev_close, prev_known),
                    (body_bottom >= prev_open, prev_known),
                ]
            )
        return out


_BREADTH_MINUTE_WINDOWS: tuple[int, ...] = (5, 30, 60)
_BREADTH_DAY_WINDOWS: tuple[int, ...] = (1, 5)
_BREADTH_EPS = 1e-4  # dead-band half-width (1 bp) on the return sign — the parity trick
_BREADTH_UNKNOWN = -1  # the unmapped-sector bucket sentinel


def _breadth_fractions(ret: np.ndarray, valid: np.ndarray) -> tuple[float, float]:
    """Market-wide up/down fractions over the VALID returns, dead-banded: up = ret > +EPS, down = ret < −EPS;
    a return within ±EPS is FLAT (in the denominator, neither up nor down). NaN denom when no valid return.
    """
    n = int(valid.sum())
    if n == 0:
        return np.nan, np.nan
    up = ((ret > _BREADTH_EPS) & valid).sum() / n
    down = ((ret < -_BREADTH_EPS) & valid).sum() / n
    return float(up), float(down)


class BreadthClean:
    """CROSS_SECTIONAL gather: what fraction of the universe (and of each ticker's SECTOR) is moving up/down
    over each horizon — market-wide + sector scalars broadcast to every present ticker. Intraday horizons
    (5m/30m/60m) use STRICT time-lag returns (close[T]/close[T−w]−1, NULL if the T−w bar is absent); daily
    horizons (1d/5d) use the prior-completed-day return from the daily snapshot. Dead-banded sign (±1bp = FLAT,
    the parity trick). present()-gated (an absent name carries a stale close → excluded). Legacy:
    ``BreadthGroup`` (FeatureGroup). Reads ``window.static['sector']`` + ``window.session['daily_close']``.
    """

    name = "breadth"
    input_cols = ("close",)
    _TAGS = tuple(f"{w}m" for w in _BREADTH_MINUTE_WINDOWS) + tuple(f"{w}d" for w in _BREADTH_DAY_WINDOWS)
    feature_names = tuple(
        f"{prefix}_{side}_{tag}"
        for tag in _TAGS
        for prefix in ("breadth", "sector_breadth")
        for side in ("up", "down", "net")
    )

    def _ret_for_tag(self, window: Window, tag: str) -> np.ndarray:
        """The per-symbol return over a horizon tag. Intraday ({w}m): strict time-lag close[T]/close[T−w]−1
        (NULL if no bar at exactly T−w). Daily ({w}d): _asof/_asof[−w]−1 where _asof = prior-completed-day
        close (daily_close[:, −2]; today is [:, −1]), so 1d = daily[−2]/daily[−3]−1, 5d = daily[−2]/daily[−7]−1.
        """
        if tag.endswith("m"):
            w = int(tag[:-1])
            close = window.trailing("close")
            minute = window.trailing_minute()
            lag_close = _close_at_lag(close, minute, window.minute_epoch, w)
            with np.errstate(invalid="ignore", divide="ignore"):
                return window.latest("close") / lag_close - 1.0
        w = int(tag[:-1])
        daily_close = window.session.get("daily_close")
        n_sym = window.n
        if daily_close is None or daily_close.shape[1] < w + 2:
            return np.full(n_sym, np.nan)
        asof = daily_close[:, -2]  # prior COMPLETED day (today = [:, -1] is excluded)
        ref = daily_close[:, -(2 + w)]
        with np.errstate(invalid="ignore", divide="ignore"):
            return asof / ref - 1.0

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        present = window.present()
        n_sym = window.n
        sector = window.static.get("sector")
        if sector is None:
            sector = np.full(n_sym, _BREADTH_UNKNOWN)
        out: dict[str, np.ndarray] = {}
        for tag in self._TAGS:
            ret = self._ret_for_tag(window, tag)
            valid = np.isfinite(ret) & present  # a valid return that delivered this minute
            up_m, down_m = _breadth_fractions(ret, valid)
            # per-sector fractions broadcast onto each member; unmapped → UNKNOWN bucket (never dropped).
            sec_up = np.full(n_sym, np.nan)
            sec_down = np.full(n_sym, np.nan)
            for sec in np.unique(sector):
                rows = sector == sec
                u, d = _breadth_fractions(ret, valid & rows)
                sec_up[rows] = u
                sec_down[rows] = d
            # emit the market scalar + the ticker's sector scalar, broadcast to every present symbol.
            out[f"breadth_up_{tag}"] = np.where(present, up_m, np.nan)
            out[f"breadth_down_{tag}"] = np.where(present, down_m, np.nan)
            out[f"breadth_net_{tag}"] = np.where(present, up_m - down_m, np.nan)
            out[f"sector_breadth_up_{tag}"] = np.where(present, sec_up, np.nan)
            out[f"sector_breadth_down_{tag}"] = np.where(present, sec_down, np.nan)
            out[f"sector_breadth_net_{tag}"] = np.where(present, sec_up - sec_down, np.nan)
        return out


class MacdClean:
    """EMA / RECURSIVE (the carried-scalar "fork" kind): MACD = 12/26-span EMAs of close + a 9-span EMA of the
    macd line. A carried decayed value per symbol, updated each present bar, in ``window.state`` — decayed on
    bar PRESENCE not clock.

    ⚠️ The PRODUCTION macd is ``technical`` (clean_groups_stateful.py, TechnicalClean) — use THAT as the value
    reference, not this. This is an interface demonstration. Both now use the ADJUSTED EWM (polars ewm_mean
    adjust=True: num/den running form), the live/legacy convention. An EARLIER version of this example used the
    SIMPLE recurrence ``(1−α)·prev + α·value`` which DIVERGES from legacy in warm-up — the convention footgun
    (the present-decay test gates on PRESENCE, not the EMA formula, so it didn't catch the wrong convention).
    Do NOT reuse the simple form."""

    name = "macd"
    input_cols = ("close",)
    feature_names = ("macd_line", "macd_signal", "macd_histogram")

    @staticmethod
    def _ema(
        state: dict[str, np.ndarray], key: str, value: np.ndarray, span: int, present: np.ndarray
    ) -> np.ndarray:
        """ADJUSTED EWM (polars ewm_mean adjust=True, the live/legacy convention): ``num = value + (1−α)·num``,
        ``den = 1 + (1−α)·den``, ``ema = num/den`` per PRESENT symbol; absent symbols hold their accumulators
        (presence-decay). First present bar seeds ema = value (num=value, den=1). NOT the simple
        ``(1−α)·prev + α·value`` recurrence — that diverges from legacy in warm-up."""
        alpha = 2.0 / (span + 1.0)
        one_minus = 1.0 - alpha
        num = state.get(f"{key}__num")
        den = state.get(f"{key}__den")
        if num is None or den is None:
            num = np.zeros(len(value))
            den = np.zeros(len(value))
        new_num = np.where(present, value + one_minus * num, num)
        new_den = np.where(present, 1.0 + one_minus * den, den)
        state[f"{key}__num"] = new_num
        state[f"{key}__den"] = new_den
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(new_den > 0.0, new_num / new_den, np.nan)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        present = (
            window.present()
        )  # the REAL delivery mask — NOT isfinite(latest), which is the carried value
        state = window.state
        ema12 = self._ema(state, "ema12", np.where(present, close, np.nan), 12, present)
        ema26 = self._ema(state, "ema26", np.where(present, close, np.nan), 26, present)
        macd_line = ema12 - ema26
        signal = self._ema(state, "signal", np.where(present, macd_line, np.nan), 9, present)
        return {
            "macd_line": macd_line,
            "macd_signal": signal,
            "macd_histogram": macd_line - signal,
        }


_INTRADAY_OPEN_MINUTE = 570  # 09:30 ET — RTH open
_INTRADAY_CLOSE_MINUTE_EXCL = 960  # 16:00 ET — RTH close (exclusive)
_INTRADAY_BUCKET = 30  # 30-minute time-of-day buckets


def _load_intraday_baseline() -> dict[int, tuple[float, float]]:
    """Eager-load the FROZEN intraday-seasonality baseline (the EXACT file legacy reads, never regenerated):
    {30-min ET bucket → (baseline_absret, vol_shape)}. Fail loud if the file is missing (house rule: no
    lazy/try-except — a missing frozen reference is a deploy error to surface, not swallow)."""
    frame = pl.read_parquet(_INTRADAY_BASELINE_PATH).select(
        pl.col("bucket").cast(pl.Int64),
        pl.col("baseline_absret").cast(pl.Float64),
        pl.col("vol_shape").cast(pl.Float64),
    )
    return {
        int(row["bucket"]): (float(row["baseline_absret"]), float(row["vol_shape"]))
        for row in frame.iter_rows(named=True)
    }


_INTRADAY_BASELINE_PATH = Path(__file__).resolve().parent / "data" / "intraday_seasonality_v1.parquet"
_INTRADAY_BASELINE: dict[int, tuple[float, float]] = _load_intraday_baseline()


class IntradaySeasonalityClean:
    """PRICE: seasonality-adjusted activity vs the time-of-day baseline. absret_vs_tod = |close/open−1| /
    baseline_absret[tod-bucket]; volume_vs_tod = volume / (the symbol's running since-open MEAN volume ·
    vol_shape[tod-bucket]). The ToD baseline is the FROZEN committed lookup (data/intraday_seasonality_v1.parquet,
    per-30min ET bucket), loaded eagerly at import. The running since-open mean volume is a per-(symbol,
    ET-session) cumulative (reset at the ET-date boundary, RTH-only). NULL outside RTH / unmapped bucket. Legacy:
    ``IntradaySeasonalityGroup``."""

    name = "intraday_seasonality"
    input_cols = ("open", "close", "volume")
    feature_names = ("absret_vs_tod", "volume_vs_tod")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        nan = np.full(n, np.nan)
        present = window.present()  # the running mean increments ONLY on a present bar
        sdate, etm = _et_session(window.minute_epoch)
        in_rth = _INTRADAY_OPEN_MINUTE <= etm < _INTRADAY_CLOSE_MINUTE_EXCL
        if not in_rth:
            return {name: nan for name in self.feature_names}
        bucket = (
            (etm - _INTRADAY_OPEN_MINUTE) // _INTRADAY_BUCKET
        ) * _INTRADAY_BUCKET + _INTRADAY_OPEN_MINUTE
        baseline = _INTRADAY_BASELINE.get(int(bucket))
        if baseline is None:
            return {name: nan for name in self.feature_names}
        baseline_absret, vol_shape = baseline

        volume = window.latest("volume")
        open_px = window.latest("open")
        close_px = window.latest("close")
        state = window.state
        # per-(symbol, ET-session) running since-open sum/count of volume — reset when the symbol crosses into a
        # new ET session (sdate change), incremented ONLY on a present RTH bar (matches the legacy cum_sum/len
        # over the RTH session partition).
        if state.get("iss_sdate") is None:
            state["iss_sum"] = np.zeros(n)
            state["iss_cnt"] = np.zeros(n)
            state["iss_sdate"] = np.full(n, -1.0)
        reset = present & (state["iss_sdate"] != float(sdate))
        state["iss_sum"] = np.where(reset, 0.0, state["iss_sum"])
        state["iss_cnt"] = np.where(reset, 0.0, state["iss_cnt"])
        state["iss_sdate"] = np.where(present, float(sdate), state["iss_sdate"])
        state["iss_sum"] = np.where(
            present, state["iss_sum"] + np.where(present, volume, 0.0), state["iss_sum"]
        )
        state["iss_cnt"] = np.where(present, state["iss_cnt"] + 1.0, state["iss_cnt"])
        with np.errstate(invalid="ignore", divide="ignore"):
            run_mean_vol = state["iss_sum"] / state["iss_cnt"]
            absret = np.abs(close_px / open_px - 1.0)
            absret_vs_tod = np.where(baseline_absret > 0.0, absret / baseline_absret, np.nan)
            volume_vs_tod = np.where(
                (run_mean_vol > 0.0) & (vol_shape > 0.0), volume / (run_mean_vol * vol_shape), np.nan
            )
        return {
            "absret_vs_tod": np.where(present, absret_vs_tod, np.nan),
            "volume_vs_tod": np.where(present, volume_vs_tod, np.nan),
        }


_SWING_THETA = 0.005  # legacy THETA — reversal threshold (0.5% fractional return) confirms a pivot
_SWING_RING_K = 8  # confirmed legs kept for persistence / alternation / resolved
_SWING_DAY_SECS = 86_400
_SWING_FIB_MAX_ABS = 10.0  # fib degenerate guard: |fib| beyond this → undefined (NULL)


class _SwingLeg:
    """One symbol's ZigZag leg-state — a faithful Python port of legacy ``swing_state._SymbolLeg``. ``advance``
    folds ONE bar point-in-time and returns the 9 features; resets the whole leg at a session-day boundary
    (per-day-backfill parity)."""

    __slots__ = (
        "cur_day",
        "direction",
        "leg_start_price",
        "leg_start_min",
        "extreme",
        "extreme_min",
        "hi",
        "hi_min",
        "lo",
        "lo_min",
        "prev_leg_start",
        "prev_leg_end",
        "have_prev_leg",
        "n_pivots_today",
        "n_alternations",
        "leg_returns",
        "leg_steeps",
    )

    def __init__(self) -> None:
        self.cur_day: int | None = None
        self.direction = 0
        self.leg_start_price = float("nan")
        self.leg_start_min = 0
        self.extreme = float("nan")
        self.extreme_min = 0
        self.hi = float("nan")
        self.hi_min = 0
        self.lo = float("nan")
        self.lo_min = 0
        self.prev_leg_start = float("nan")
        self.prev_leg_end = float("nan")
        self.have_prev_leg = False
        self.n_pivots_today = 0.0
        self.n_alternations = 0.0
        self.leg_returns: list[float] = []
        self.leg_steeps: list[float] = []

    def _push_pivot(self, pivot_price: float, start_price: float, span_secs: int) -> None:
        signed_ret = (pivot_price - start_price) / start_price if start_price > 0.0 else 0.0
        mins = span_secs // 60
        steep = signed_ret / mins if mins > 0 else 0.0
        self.prev_leg_start = start_price
        self.prev_leg_end = pivot_price
        self.have_prev_leg = True
        self.leg_returns.append(signed_ret)
        self.leg_steeps.append(steep)
        del self.leg_returns[:-_SWING_RING_K]
        del self.leg_steeps[:-_SWING_RING_K]

    def advance(self, close: float, minute: int) -> tuple[float, ...]:
        day = minute // _SWING_DAY_SECS
        if self.cur_day is not None and day != self.cur_day:
            # CONTINUOUS-FOLD (the Rust swing_fold / backfill compute(), which the clean engine matches) only
            # resets the PER-DAY pivot COUNT at the day boundary — the leg geometry, the pivot ring
            # (n_alternations / leg_returns / leg_steeps / persistence), and minutes_since_pivot all CARRY
            # across the overnight gap. (The full _reset_for_new_session is the STATEFUL-LIVE per-day twin, a
            # DIFFERENT semantic that matches a per-day-materialized backfill; the clean engine is one
            # continuous replay = the whole-buffer fold, so it must NOT full-reset here.)
            self.n_pivots_today = 0.0
        self.cur_day = day
        if self.leg_start_price != self.leg_start_price:  # NaN → first bar of the (re)started session block
            self.leg_start_price = close
            self.leg_start_min = minute
            self.extreme = close
            self.extreme_min = minute
            self.hi = self.lo = close
            self.hi_min = self.lo_min = minute
        elif self.direction == 0:
            if close > self.hi:
                self.hi, self.hi_min = close, minute
            if close < self.lo:
                self.lo, self.lo_min = close, minute
            down_rev = (self.hi - close) / self.hi if self.hi > 0.0 else 0.0
            up_rev = (close - self.lo) / self.lo if self.lo > 0.0 else 0.0
            if down_rev >= _SWING_THETA and down_rev >= up_rev:
                self._push_pivot(self.hi, self.leg_start_price, self.hi_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = -1
                self.leg_start_price, self.leg_start_min = self.hi, self.hi_min
                self.extreme, self.extreme_min = close, minute
            elif up_rev >= _SWING_THETA:
                self._push_pivot(self.lo, self.leg_start_price, self.lo_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = 1
                self.leg_start_price, self.leg_start_min = self.lo, self.lo_min
                self.extreme, self.extreme_min = close, minute
        elif self.direction == 1:
            if close >= self.extreme:
                self.extreme, self.extreme_min = close, minute
            elif self.extreme > 0.0 and (self.extreme - close) / self.extreme >= _SWING_THETA:
                self._push_pivot(self.extreme, self.leg_start_price, self.extreme_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = -1
                self.leg_start_price, self.leg_start_min = self.extreme, self.extreme_min
                self.extreme, self.extreme_min = close, minute
        else:  # direction == -1
            if close <= self.extreme:
                self.extreme, self.extreme_min = close, minute
            elif self.extreme > 0.0 and (close - self.extreme) / self.extreme >= _SWING_THETA:
                self._push_pivot(self.extreme, self.leg_start_price, self.extreme_min - self.leg_start_min)
                self.n_pivots_today += 1.0
                self.n_alternations += 1.0
                self.direction = 1
                self.leg_start_price, self.leg_start_min = self.extreme, self.extreme_min
                self.extreme, self.extreme_min = close, minute
        len_pct = (
            (close - self.leg_start_price) / self.leg_start_price if self.leg_start_price > 0.0 else 0.0
        )
        mins = (minute - self.leg_start_min) // 60
        steep = len_pct / mins if mins > 0 else 0.0
        msp = float(mins) if self.direction != 0 else float("nan")
        persistence = sum(self.leg_returns) + len_pct
        if self.have_prev_leg and abs(self.prev_leg_start - self.prev_leg_end) > 0.0:
            fib = (close - self.prev_leg_end) / (self.prev_leg_start - self.prev_leg_end)
            fib = fib if abs(fib) <= _SWING_FIB_MAX_ABS else float("nan")
        else:
            fib = float("nan")
        resolved = 0.0
        if len(self.leg_returns) >= 2 and self.direction != 0:
            max_prior_len = max(abs(x) for x in self.leg_returns)
            max_prior_steep = max(abs(x) for x in self.leg_steeps)
            persists = (len_pct > 0.0 and self.direction == 1) or (len_pct < 0.0 and self.direction == -1)
            if persists and abs(len_pct) > max_prior_len and abs(steep) > max_prior_steep:
                resolved = 1.0
        return (
            float(self.direction),
            steep,
            len_pct,
            msp,
            self.n_pivots_today,
            self.n_alternations,
            persistence,
            fib,
            resolved,
        )


class SwingClean:
    """TREND_QUALITY / STATEFUL: a point-in-time ZigZag swing/leg state machine per symbol. The 9 swing features
    (swing_dir/steepness/len_pct, minutes_since_pivot, n_pivots_today/alternations, swing_persistence,
    fib_retracement, trend_resolved) from a per-symbol carried ``_SwingLeg`` advanced one bar per present minute
    (O(1)/bar, no buffer re-scan). Resets the leg at the session-day boundary (per-day-backfill parity). A
    faithful port of legacy ``SwingGroup`` + ``swing_state._SymbolLeg``."""

    name = "swing"
    input_cols = ("close",)
    feature_names = (
        "swing_dir",
        "swing_steepness",
        "swing_len_pct",
        "minutes_since_pivot",
        "n_pivots_today",
        "n_alternations",
        "swing_persistence",
        "fib_retracement",
        "trend_resolved",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        present = window.present()  # the leg advances ONLY on a present bar (absent → carries state, no row)
        minute_epoch = window.minute_epoch
        n = window.n
        legs_obj = window.state.get("legs")
        if legs_obj is None:
            # one carried _SwingLeg per symbol, held in an object-dtype array (fits the state dict's ndarray
            # contract while carrying the per-symbol Python state machines).
            legs_obj = np.array([_SwingLeg() for _ in range(n)], dtype=object)
            window.state["legs"] = legs_obj
        rows = np.full((n, len(self.feature_names)), np.nan)
        for i in range(n):
            if present[i] and np.isfinite(close[i]):
                rows[i] = legs_obj[i].advance(float(close[i]), int(minute_epoch))
        out: dict[str, np.ndarray] = {}
        for j, name in enumerate(self.feature_names):
            out[name] = np.where(present, rows[:, j], np.nan)
        return out


_PRIOR_DAY_PIVOTS = ("p", "r1", "s1", "r2", "s2")


class PriorDayClean:
    """DAILY-SNAPSHOT (MULTI_DAY): prior-day reference LEVELS (the floor-trader pivots P/R1/S1/R2/S2 from the
    prior day's OHLC + the prior H/L/C) and the at-T close's distance from each, plus the overnight gap. All
    levels are anchored to the LAST COMPLETED daily bar (D-1) — the daily snapshot's second-newest column
    (``[:, -2]``); today's daily open (``[:, -1]``) supplies gap_open. The close-relative distances read the
    live minute close. Legacy: ``PriorDayGroup`` (DailySnapshotGroup)."""

    name = "prior_day"
    input_cols = ("close",)
    feature_names = (
        "gap_open",
        "dist_from_prior_high",
        "dist_from_prior_low",
        "dist_from_prior_close",
        "above_pivot",
    ) + tuple(f"dist_from_pivot_{pivot}" for pivot in _PRIOR_DAY_PIVOTS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        nan = np.full(n, np.nan)
        daily_open = window.session.get("daily_open")
        daily_high = window.session.get("daily_high")
        daily_low = window.session.get("daily_low")
        daily_close = window.session.get("daily_close")
        if (
            daily_open is None
            or daily_high is None
            or daily_low is None
            or daily_close is None
            or daily_close.shape[1] < 2
        ):
            return {name: nan for name in self.feature_names}
        # D-1 (last COMPLETED daily bar) levels = the second-newest daily column; today's open = newest.
        today_open = daily_open[:, -1]
        prev_high = daily_high[:, -2]
        prev_low = daily_low[:, -2]
        prev_close = daily_close[:, -2]
        with np.errstate(invalid="ignore", divide="ignore"):
            pivot = (prev_high + prev_low + prev_close) / 3.0
            span = prev_high - prev_low
            levels = {
                "p": pivot,
                "r1": 2.0 * pivot - prev_low,
                "s1": 2.0 * pivot - prev_high,
                "r2": pivot + span,
                "s2": pivot - span,
            }
            close = window.latest("close")  # the at-T minute close
            out: dict[str, np.ndarray] = {
                "gap_open": today_open / prev_close - 1.0,
                "dist_from_prior_high": close / prev_high - 1.0,
                "dist_from_prior_low": close / prev_low - 1.0,
                "dist_from_prior_close": close / prev_close - 1.0,
                "above_pivot": (close > pivot).astype(np.float64),
            }
            for pivot_name in _PRIOR_DAY_PIVOTS:
                out[f"dist_from_pivot_{pivot_name}"] = close / levels[pivot_name] - 1.0
        return out
