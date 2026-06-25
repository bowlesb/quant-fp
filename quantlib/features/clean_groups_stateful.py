"""Genuinely carried-state feature groups ported to the ``CleanEngine`` interface.

These carry per-symbol state across minutes in ``window.state`` (the engine's per-group memo handed back each
step) and update it ONLY on a present bar (presence-decay, NOT clock-decay; an absent symbol's accumulators are
untouched). The minute-epoch watermark makes a re-delivered minute a no-op so the state never double-advances.

THE ADJUSTED-EWM CONVENTION (the recursive kind): the live/legacy EMA is polars ``ewm_mean(adjust=True)`` =
``num_t = x + (1−α)·num``, ``den_t = 1 + (1−α)·den``, ``ema = num/den`` (α=2/(span+1)), NOT the simple
``(1−α)·prev + α·x`` recurrence (they diverge in warm-up). Matched exactly here.
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window
from quantlib.features.clean_groups_windowed import _masked_mean, _masked_std, _windowed_sum


def _adjusted_ema(
    state: dict[str, np.ndarray], key: str, value: np.ndarray, span: float, present: np.ndarray
) -> np.ndarray:
    """One step of the ADJUSTED EWM (polars ewm_mean adjust=True) per symbol, updated only on present bars:
    ``num = value + (1−α)·num``, ``den = 1 + (1−α)·den``, ``ema = num/den``. The (num, den) accumulators live in
    ``state`` under ``key__num``/``key__den`` (start at 0 → first present bar seeds ema = value). An absent
    symbol's accumulators are untouched (presence-decay)."""
    alpha = 2.0 / (span + 1.0)
    one_minus = 1.0 - alpha
    n = len(value)
    num = state.get(f"{key}__num")
    den = state.get(f"{key}__den")
    if num is None:
        num = np.zeros(n)
        den = np.zeros(n)
    new_num = np.where(present, value + one_minus * num, num)
    new_den = np.where(present, 1.0 + one_minus * den, den)
    state[f"{key}__num"] = new_num
    state[f"{key}__den"] = new_den
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(new_den > 0.0, new_num / new_den, np.nan)


_SMA_WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 50, 100, 200)
_BB_REL_EPS = 1e-6


class TechnicalClean:
    """TECHNICAL: RSI (windowed gain/loss), MACD (recursive ADJUSTED EWM), Bollinger (windowed sma/std),
    SMA distances (windowed mean). rsi_14m, macd_line/signal/hist, bb_position_20m, bb_width_20m, sma_dist_{w}m.
    Legacy: ``TechnicalGroup`` — a hybrid (windowed reductions + the recursive MACD EMA). The MACD uses the
    ADJUSTED num/den EWM (matches polars ewm_mean adjust=True), present-bar decay."""

    name = "technical"
    input_cols = ("close",)
    feature_names = (
        "rsi_14m",
        "macd_line",
        "macd_signal",
        "macd_hist",
        "bb_position_20m",
        "bb_width_20m",
    ) + tuple(f"sma_dist_{w}m" for w in _SMA_WINDOWS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close_buf = window.trailing("close")
        close = window.latest("close")
        present = window.present()
        state = window.state

        # MACD — recursive ADJUSTED EWM of close (12/26 spans) + 9-span EMA of the macd line.
        ema12 = _adjusted_ema(state, "ema12", np.where(present, close, np.nan), 12, present)
        ema26 = _adjusted_ema(state, "ema26", np.where(present, close, np.nan), 26, present)
        macd_line = ema12 - ema26
        macd_signal = _adjusted_ema(state, "signal", np.where(present, macd_line, np.nan), 9, present)

        # RSI_14m — windowed gain/loss sums over the trailing 14 per-bar price changes.
        with np.errstate(invalid="ignore"):
            diff = close_buf[:, 1:] - close_buf[:, :-1]
            diff = np.concatenate([np.full((close_buf.shape[0], 1), np.nan), diff], axis=1)
            gain = np.where(diff > 0.0, diff, 0.0)
            loss = np.where(diff < 0.0, -diff, 0.0)
        # restore NaN where the diff itself is NaN (no prior bar) so warm-up bars don't count as 0-gain
        gain = np.where(np.isfinite(diff), gain, np.nan)
        loss = np.where(np.isfinite(diff), loss, np.nan)
        sum_gain = _windowed_sum(gain, 14)
        sum_loss = _windowed_sum(loss, 14)
        total = sum_gain + sum_loss
        with np.errstate(invalid="ignore", divide="ignore"):
            rsi = np.where(total > 0.0, np.clip(100.0 * sum_gain / total, 0.0, 100.0), np.nan)

        # Bollinger (20m): position = (close − sma20)/(2·std20); width = 4·std20/sma20. Relative-eps + finite
        # guard so a degenerate flat window is NULL (matches legacy bb_well_defined).
        sma20, _ = _masked_mean(close_buf, 20)
        std20 = _masked_std(close_buf, 20)
        with np.errstate(invalid="ignore", divide="ignore"):
            bb_ok = np.isfinite(std20) & (std20 > _BB_REL_EPS * np.abs(sma20)) & (np.abs(sma20) > 0.0)
            bb_position = np.where(bb_ok, (close - sma20) / (2.0 * std20), np.nan)
            bb_width = np.where(bb_ok, 4.0 * std20 / sma20, np.nan)

        out: dict[str, np.ndarray] = {
            "rsi_14m": rsi,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_line - macd_signal,
            "bb_position_20m": bb_position,
            "bb_width_20m": bb_width,
        }
        for w in _SMA_WINDOWS:
            sma_w, _ = _masked_mean(close_buf, w)
            with np.errstate(invalid="ignore", divide="ignore"):
                out[f"sma_dist_{w}m"] = close / sma_w - 1.0
        return out
