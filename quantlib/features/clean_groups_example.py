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

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)


def _trailing_window(mat: np.ndarray, w: int) -> np.ndarray:
    """The last ``w`` columns of the (n_symbols, window) trailing matrix — each symbol's most recent ``w``
    present bars (NaN-padded on the left where it has fewer)."""
    return mat[:, -w:]


class TrendQualityClean:
    """Trailing OLS of close on time over each window: normalized slope + R². The same math
    ``trend_quality`` declared via rolling sums, now one numpy function over the carried close window.

    For each window ``w``: x = 0..w-1 (minutes), y = the trailing ``w`` closes. slope = cov(x,y)/var(x);
    r2 = cov(x,y)²/(var(x)·var(y)). NaN where the window isn't filled (fewer than ``w`` present bars) — the
    feature's own warm-up, exactly as a short backfill window leaves it."""

    name = "trend_quality"
    input_cols = ("close",)
    feature_names = tuple(
        f"{stat}_{w}m" for w in WINDOWS for stat in ("price_slope", "price_r2", "trend_strength")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.trailing("close")  # (n_symbols, window), oldest→newest
        out: dict[str, np.ndarray] = {}
        for w in WINDOWS:
            y = _trailing_window(close, w)  # (n, w)
            n_present = np.sum(np.isfinite(y), axis=1)  # per-symbol filled count in this window
            x = np.arange(w, dtype=np.float64)[None, :]  # (1, w) time axis, broadcast
            mask = np.isfinite(y)
            yf = np.where(mask, y, 0.0)
            xf = np.where(mask, x, 0.0)
            nb = n_present.astype(np.float64)
            # masked sums over the present bars (a bar missing at the window edge drops from the fit)
            sx = xf.sum(axis=1)
            sy = yf.sum(axis=1)
            sxx = (xf * xf).sum(axis=1)
            syy = (yf * yf).sum(axis=1)
            sxy = (xf * yf).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                cov = nb * sxy - sx * sy
                var_x = nb * sxx - sx * sx
                var_y = nb * syy - sy * sy
                slope = cov / var_x
                mean_y = sy / nb
                norm_slope = slope / mean_y  # fractional move per minute
                r2 = (cov * cov) / (var_x * var_y)
            valid = n_present >= 2
            norm_slope = np.where(valid & (var_x > 0), norm_slope, np.nan)
            r2 = np.where(valid & (var_x > 0) & (var_y > 0), np.clip(r2, 0.0, 1.0), np.nan)
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
        out: dict[str, np.ndarray] = {}
        for w in WINDOWS:
            c = _trailing_window(close, w)
            v = _trailing_window(volume, w)
            mask = np.isfinite(c) & np.isfinite(v)
            cv = np.where(mask, c * v, 0.0).sum(axis=1)
            vol = np.where(mask, v, 0.0).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = cv / vol
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
            r = _trailing_window(rng, w)
            mask = np.isfinite(r)
            n_present = mask.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                mean_rng = np.where(mask, r, 0.0).sum(axis=1) / n_present
            out[f"realized_range_{w}m"] = np.where(n_present > 0, mean_rng, np.nan)
        return out


class CandlestickClean:
    """Per-bar candlestick geometry + the two-candle engulfing pattern (reads the prior bar). A DIFFERENT shape
    from the rolling/windowed groups — per-bar arithmetic on the latest OHLC + a lag-1 read — proving the
    interface covers bespoke per-bar/lag features, not just windowed reductions."""

    name = "candlestick"
    input_cols = ("open", "high", "low", "close")
    feature_names = (
        "body_ratio",
        "upper_shadow_ratio",
        "lower_shadow_ratio",
        "is_doji",
        "pattern_engulfing_bullish",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        o = window.latest("open")
        h = window.latest("high")
        low = window.latest("low")
        c = window.latest("close")
        rng = h - low
        valid = rng > 0
        with np.errstate(invalid="ignore", divide="ignore"):
            body = np.abs(c - o) / rng
            upper = (h - np.maximum(o, c)) / rng
            lower = (np.minimum(o, c) - low) / rng
        body = np.where(valid, body, 0.0)
        upper = np.where(valid, upper, 0.0)
        lower = np.where(valid, lower, 0.0)
        is_doji = (body < 0.10).astype(np.float64)
        # two-candle bullish engulfing: this bar bullish (c>o), prior bar bearish, this body engulfs prior body.
        prior = _trailing_window(window.trailing("close"), 2)  # (n, 2): [prior_close, this_close]
        prior_close = prior[:, 0]
        prior_open = _trailing_window(window.trailing("open"), 2)[:, 0]
        this_bull = c > o
        prior_bear = prior_close < prior_open
        engulf = this_bull & prior_bear & (c >= prior_open) & (o <= prior_close)
        return {
            "body_ratio": body,
            "upper_shadow_ratio": upper,
            "lower_shadow_ratio": lower,
            "is_doji": is_doji,
            "pattern_engulfing_bullish": engulf.astype(np.float64),
        }


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
        out: dict[str, np.ndarray] = {}
        for w in (5, 10, 30):
            cw = _trailing_window(close, w)
            # per-symbol return over the window (newest / oldest-present − 1)
            newest = cw[:, -1]
            oldest = cw[:, 0]
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = newest / oldest - 1.0
            valid = np.isfinite(ret)
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
    macd line. NOT a windowed read — a carried decayed value per symbol, updated each present bar. Lives in
    ``window.state`` (the group's own carried state the engine hands back each minute), decayed on bar
    PRESENCE not clock — proving the interface covers the recursive kind via per-group carried state."""

    name = "macd"
    input_cols = ("close",)
    feature_names = ("macd_line", "macd_signal", "macd_histogram")

    @staticmethod
    def _ema(
        state: dict[str, np.ndarray], key: str, value: np.ndarray, span: int, present: np.ndarray
    ) -> np.ndarray:
        """Carried EMA: ``v = (1−α)·v + α·value`` per present symbol; absent symbols hold (decay on presence,
        not clock). Seeds to the first present value. Stored in ``state`` so it persists across steps."""
        alpha = 2.0 / (span + 1.0)
        prev = state.get(key)
        if prev is None:
            prev = np.full(len(value), np.nan)
        updated = np.where(np.isnan(prev), value, (1.0 - alpha) * prev + alpha * value)
        new = np.where(present, updated, prev)  # absent symbols keep their last EMA (presence-decay)
        state[key] = new
        return new

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        present = np.isfinite(close)
        state = window.state
        ema12 = self._ema(state, "ema12", np.where(present, close, 0.0), 12, present)
        ema26 = self._ema(state, "ema26", np.where(present, close, 0.0), 26, present)
        macd_line = ema12 - ema26
        signal = self._ema(state, "signal", np.where(present, macd_line, 0.0), 9, present)
        return {
            "macd_line": macd_line,
            "macd_signal": signal,
            "macd_histogram": macd_line - signal,
        }
