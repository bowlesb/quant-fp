"""Worked examples: feature groups ported to the one ``CleanEngine`` interface.

These show the migration shape — a group's math becomes ONE small numpy ``compute(window)`` over the carried
buffer, replacing its legacy ``reduced()/regressions()/assemble()`` declarative form (or its bespoke polars
``compute()``). The arithmetic is the same; it is expressed once, framework-free, and the live step and the
backfill are the same replay through the engine. (The full migration ports all ~68 groups this way; these four
prove the interface covers the diverse shapes: a rolling OLS, a windowed volume-weighted ratio, a windowed mean
of a per-bar derived quantity, and per-bar candlestick geometry + a two-candle lag-1 pattern.)
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window
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


class BreadthClean:
    """CROSS-SECTIONAL: what fraction of the universe is moving up/down over each window. A reduce over the
    SYMBOL axis (not a per-symbol window) — a market-wide scalar broadcast to every ticker. Proves the
    interface covers the cross-sectional "fork" kind: ``compute`` already sees all symbols' matrices, so the
    reduce is a numpy ``mean`` over axis 0. (The sector-grouped variant uses ``window.static['sector']``.)"""

    name = "breadth"
    input_cols = ("close",)
    feature_names = tuple(f"breadth_{d}_{w}" for w in (5, 10, 30) for d in ("up", "down", "net"))

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")
        present = (
            window.present()
        )  # the REAL delivery mask — an absent symbol must NOT enter the denominator
        out: dict[str, np.ndarray] = {}
        for w in (5, 10, 30):
            cw = _trailing_window(close, w)
            # per-symbol return over the window (newest / oldest-present − 1)
            newest = cw[:, -1]
            oldest = cw[:, 0]
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = newest / oldest - 1.0
            # gate on present(): a symbol that delivered no bar this minute has a finite trailing return from
            # its CARRIED close, so isfinite(ret) alone wrongly counts it. Breadth is "fraction of the universe
            # moving up RIGHT NOW" — only symbols present this minute participate.
            valid = np.isfinite(ret) & present
            band = 1e-4  # dead-band: a name within ±band is neither up nor down (robust count, breadth.py)
            up = (ret > band) & valid
            down = (ret < -band) & valid
            n = max(int(valid.sum()), 1)
            frac_up = float(up.sum()) / n  # market-wide scalar
            frac_down = float(down.sum()) / n
            full = np.full(window.n, np.nan)  # broadcast the scalar to every symbol
            out[f"breadth_up_{w}"] = np.where(valid, frac_up, np.nan) if valid.any() else full
            out[f"breadth_down_{w}"] = np.where(valid, frac_down, np.nan) if valid.any() else full
            out[f"breadth_net_{w}"] = np.where(valid, frac_up - frac_down, np.nan) if valid.any() else full
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


class IntradaySeasonalityClean:
    """CUMULATIVE / session-reset: volume vs the symbol's running since-open mean volume. A carried running
    sum + count per symbol, RESET at the session open (``minute_epoch`` crossing a new day). Lives in
    ``window.state`` like the EMA, but the carry is a running mean that resets — proving the cumulative kind.
    """

    name = "intraday_seasonality"
    input_cols = ("volume",)
    feature_names = ("volume_vs_session_mean",)
    _SESSION_SECONDS = 86400  # day bucket for the reset (a real impl uses the exchange session boundary)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        volume = window.latest("volume")
        present = (
            window.present()
        )  # the REAL delivery mask — the count must NOT increment on an absent minute
        state = window.state
        day = window.minute_epoch // self._SESSION_SECONDS
        # reset the running sum/count when the session day changes
        if state.get("day") is None or int(state["day"][0]) != day:
            state["sum"] = np.zeros(window.n)
            state["cnt"] = np.zeros(window.n)
            state["day"] = np.full(window.n, day, dtype=np.float64)
        state["sum"] = np.where(present, state["sum"] + np.where(present, volume, 0.0), state["sum"])
        state["cnt"] = np.where(present, state["cnt"] + 1.0, state["cnt"])
        with np.errstate(invalid="ignore", divide="ignore"):
            session_mean = state["sum"] / state["cnt"]
            ratio = volume / session_mean
        return {"volume_vs_session_mean": np.where(present & (state["cnt"] > 0), ratio, np.nan)}


class SwingClean:
    """STATEFUL / swing: a ZigZag leg-state machine per symbol — the current leg direction + running extreme,
    a pivot when price reverses by >= theta from the leg extreme. O(1) per bar, carried in ``window.state``
    (no buffer re-scan) — proving the small-per-symbol-state-machine kind. (Simplified single-leg ZigZag; the
    production group adds the Fibonacci leg sequence, same carried-state shape.)"""

    name = "swing"
    input_cols = ("close",)
    feature_names = ("swing_direction", "swing_leg_return", "swing_pivot")
    _THETA = 0.01  # 1% reversal confirms a pivot

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        present = window.present()  # the REAL delivery mask — the leg must NOT advance on an absent minute
        state = window.state
        n = window.n
        if state.get("extreme") is None:
            state["extreme"] = np.where(present, close, np.nan)  # the running leg extreme
            state["dir"] = np.zeros(n)  # +1 up-leg, -1 down-leg, 0 undecided
            state["leg_start"] = np.where(present, close, np.nan)
        extreme = state["extreme"]
        direction = state["dir"]
        pivot = np.zeros(n)
        with np.errstate(invalid="ignore", divide="ignore"):
            up_move = (close - extreme) / extreme  # vs the current extreme
        # up-leg: extreme tracks the high; a fall of >= theta from it confirms a down pivot (and vice versa).
        new_extreme = extreme.copy()
        new_dir = direction.copy()
        new_leg_start = state["leg_start"].copy()
        for i in range(n):
            if not present[i] or not np.isfinite(extreme[i]):
                if present[i] and not np.isfinite(extreme[i]):
                    new_extreme[i] = close[i]
                    new_leg_start[i] = close[i]
                continue
            d = direction[i]
            mv = up_move[i]
            if d >= 0 and close[i] >= extreme[i]:
                new_extreme[i] = close[i]  # extending the up-leg's high
                new_dir[i] = 1
            elif d <= 0 and close[i] <= extreme[i]:
                new_extreme[i] = close[i]  # extending the down-leg's low
                new_dir[i] = -1
            elif d == 1 and mv <= -self._THETA:  # up-leg reversed down by theta → down pivot
                pivot[i] = 1.0
                new_dir[i] = -1
                new_leg_start[i] = extreme[i]
                new_extreme[i] = close[i]
            elif d == -1 and mv >= self._THETA:  # down-leg reversed up by theta → up pivot
                pivot[i] = 1.0
                new_dir[i] = 1
                new_leg_start[i] = extreme[i]
                new_extreme[i] = close[i]
        state["extreme"] = new_extreme
        state["dir"] = new_dir
        state["leg_start"] = new_leg_start
        with np.errstate(invalid="ignore", divide="ignore"):
            leg_return = close / new_leg_start - 1.0
        return {
            "swing_direction": np.where(present, new_dir, np.nan),
            "swing_leg_return": np.where(present, leg_return, np.nan),
            "swing_pivot": np.where(present, pivot, np.nan),
        }


class PriorDayClean:
    """DAILY-SNAPSHOT: intraday-invariant prior-day reference levels (prior close), computed ONCE per session
    and broadcast to every minute. Reads ``window.session`` (the engine's per-session memo) instead of the
    rolling window — proving the snapshot kind. ``gap_from_prior_close`` = today's latest / prior_close − 1.
    """

    name = "prior_day"
    input_cols = ("close",)
    feature_names = ("gap_from_prior_close",)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        prior_close = window.session.get("prior_close")
        close = window.latest("close")
        if prior_close is None:
            return {"gap_from_prior_close": np.full(window.n, np.nan)}
        with np.errstate(invalid="ignore", divide="ignore"):
            gap = close / prior_close - 1.0
        # gap is emitted only for a symbol that delivered a bar this minute (present), not on a carried close
        return {"gap_from_prior_close": np.where(window.present() & np.isfinite(close), gap, np.nan)}
