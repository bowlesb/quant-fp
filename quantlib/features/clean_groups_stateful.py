"""Genuinely carried-state feature groups ported to the ``CleanEngine`` interface.

These carry per-symbol state across minutes in ``window.state`` (the engine's per-group memo handed back each
step) and update it ONLY on a present bar (presence-decay, NOT clock-decay; an absent symbol's accumulators are
untouched). The minute-epoch watermark makes a re-delivered minute a no-op so the state never double-advances.

THE ADJUSTED-EWM CONVENTION (the recursive kind): the live/legacy EMA is polars ``ewm_mean(adjust=True)`` =
``num_t = x + (1−α)·num``, ``den_t = 1 + (1−α)·den``, ``ema = num/den`` (α=2/(span+1)), NOT the simple
``(1−α)·prev + α·x`` recurrence (they diverge in warm-up). Matched exactly here.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from quantlib.features.clean_engine import Window
from quantlib.features.clean_groups_windowed import _masked_mean, _masked_std, _windowed_sum

_ET = ZoneInfo("America/New_York")
_OPEN_MINUTE = 570  # 09:30 ET — the session-cumulative groups confine to RTH (et_minute_of_day >= this)


def _et_session(minute_epoch: int) -> tuple[int, int]:
    """The (ET ordinal-date, ET minute-of-day) for ``minute_epoch`` — the session-reset key + the RTH gate.
    Date ordinal changes → a new ET session → the cumulative accumulators reset."""
    et = dt.datetime.fromtimestamp(minute_epoch, _ET)
    return et.toordinal(), et.hour * 60 + et.minute


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

_RUNNER_BAND_LO = 2.0
_RUNNER_BAND_HI = 20.0
_RUNNER_ACTIVE_EARLY_MOVE = 0.30


def _update_session_cumulative(window: Window) -> dict[str, np.ndarray]:
    """Maintain the per-(symbol, ET-session) cumulative accumulators in ``window.state`` and return the current
    ``{run_high, run_low, run_dollar, sess_open, close}``. Shared by the runner/dumper/gap_fill session-reset
    machines. Updated ONLY on a present RTH bar (etm≥570); RESET on a symbol's first present bar of a new ET
    session (sdate change); an absent symbol's accumulators are untouched (present-decay). Pre-market bars
    (etm<570) don't update."""
    n = window.n
    state = window.state
    present = window.present()
    sdate, etm = _et_session(window.minute_epoch)
    op = window.latest("open")
    high = window.latest("high")
    low = window.latest("low")
    close = window.latest("close")
    volume = window.latest("volume")

    if state.get("run_high") is None:
        state["run_high"] = np.full(n, np.nan)
        state["run_low"] = np.full(n, np.nan)
        state["run_dollar"] = np.zeros(n)
        state["sess_open"] = np.full(n, np.nan)
        state["sdate"] = np.full(n, -1.0)
    run_high, run_low = state["run_high"], state["run_low"]
    run_dollar, sess_open, prev_sdate = state["run_dollar"], state["sess_open"], state["sdate"]

    update = present & (etm >= _OPEN_MINUTE)
    new_session = update & (prev_sdate != float(sdate))
    with np.errstate(invalid="ignore"):
        dollar = close * volume
        new_run_high = np.where(new_session, high, np.where(update, np.fmax(run_high, high), run_high))
        new_run_low = np.where(new_session, low, np.where(update, np.fmin(run_low, low), run_low))
        new_run_dollar = np.where(new_session, dollar, np.where(update, run_dollar + dollar, run_dollar))
        new_sess_open = np.where(new_session, op, sess_open)
    state["run_high"] = new_run_high
    state["run_low"] = new_run_low
    state["run_dollar"] = new_run_dollar
    state["sess_open"] = new_sess_open
    state["sdate"] = np.where(update, float(sdate), prev_sdate)
    return {
        "run_high": new_run_high,
        "run_low": new_run_low,
        "run_dollar": new_run_dollar,
        "sess_open": new_sess_open,
        "close": close,
    }


class RunnerStateClean:
    """CUMULATIVE SESSION-RESET (the small-cap-runner regime): per (symbol, ET-session), the running max-high /
    cum dollar-volume / first session open since the 09:30 ET open, vs the prior-day close. Resets every
    session. runner_early_move = run_high/prev_close−1; runner_gap_open = sess_open/prev_close−1;
    runner_pullback_from_high = close/run_high−1; runner_log_dollar_vol = log1p(run_dollar); runner_in_band =
    prev_close ∈ [2,20]; runner_is_active = in_band & early_move≥0.30. Legacy: ``RunnerStateGroup``.

    Carried in window.state: run_high / run_dollar / sess_open / sdate, per symbol. Updated ONLY on a PRESENT
    RTH bar; RESET on a symbol's FIRST present bar of a new ET session (sdate change). An absent symbol's
    accumulators are untouched (present-decay) — it carries its session state through an absent minute, and a
    new session only resets it once it next prints (matching the legacy per-(symbol,sdate) re-reduce). Reads
    ``window.session['prev_close']`` (the prior-day close)."""

    name = "runner_state"
    input_cols = ("open", "high", "low", "close", "volume")
    feature_names = (
        "runner_early_move",
        "runner_gap_open",
        "runner_pullback_from_high",
        "runner_log_dollar_vol",
        "runner_in_band",
        "runner_is_active",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        acc = _update_session_cumulative(window)
        run_high, sess_open, run_dollar, close = (
            acc["run_high"],
            acc["sess_open"],
            acc["run_dollar"],
            acc["close"],
        )
        prev_close = window.session.get("prev_close")
        if prev_close is None:
            prev_close = np.full(window.n, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            early_move = run_high / prev_close - 1.0
            in_band = (prev_close >= _RUNNER_BAND_LO) & (prev_close <= _RUNNER_BAND_HI)
            is_active = in_band & (early_move >= _RUNNER_ACTIVE_EARLY_MOVE)
            return {
                "runner_early_move": early_move,
                "runner_gap_open": sess_open / prev_close - 1.0,
                "runner_pullback_from_high": close / run_high - 1.0,
                "runner_log_dollar_vol": np.log1p(run_dollar),
                "runner_in_band": in_band.astype(np.float64),
                "runner_is_active": is_active.astype(np.float64),
            }


class DumperStateClean:
    """CUMULATIVE SESSION-RESET (the small-cap-crash regime, the cum-MIN mirror of runner): per (symbol,
    ET-session) the running min-low / cum dollar-vol / first open since 09:30 ET, vs prior close. dumper_early_
    drop = 1 − run_low/prev_close (running max drop); dumper_gap_open = sess_open/prev_close−1;
    dumper_bounce_from_low = close/run_low−1; dumper_log_dollar_vol = log1p(run_dollar); dumper_in_band =
    prev_close ∈ [2,20]; dumper_is_active = in_band & early_drop≥0.30. Legacy: ``DumperStateGroup``. Same
    session-reset machinery as runner (shared ``_update_session_cumulative``)."""

    name = "dumper_state"
    input_cols = ("open", "high", "low", "close", "volume")
    feature_names = (
        "dumper_early_drop",
        "dumper_gap_open",
        "dumper_bounce_from_low",
        "dumper_log_dollar_vol",
        "dumper_in_band",
        "dumper_is_active",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        acc = _update_session_cumulative(window)
        run_low, sess_open, run_dollar, close = (
            acc["run_low"],
            acc["sess_open"],
            acc["run_dollar"],
            acc["close"],
        )
        prev_close = window.session.get("prev_close")
        if prev_close is None:
            prev_close = np.full(window.n, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            early_drop = 1.0 - run_low / prev_close
            in_band = (prev_close >= _RUNNER_BAND_LO) & (prev_close <= _RUNNER_BAND_HI)
            is_active = in_band & (early_drop >= _RUNNER_ACTIVE_EARLY_MOVE)
            return {
                "dumper_early_drop": early_drop,
                "dumper_gap_open": sess_open / prev_close - 1.0,
                "dumper_bounce_from_low": close / run_low - 1.0,
                "dumper_log_dollar_vol": np.log1p(run_dollar),
                "dumper_in_band": in_band.astype(np.float64),
                "dumper_is_active": is_active.astype(np.float64),
            }


class GapFillStateClean:
    """SESSION-RESET (the overnight-gap-fill regime): the running fraction of the overnight gap filled by this
    minute. gap_fill_fraction = (close − sess_open)/(prev_close − sess_open) — 0 at the open, 1.0 = back to
    prev_close (filled), <0 = extended past the open; NULL on a zero-gap day (|denom| ≤ 1e-9). gap_extended
    (Int8) = 1 if fraction < 0. Legacy: ``GapFillStateGroup``. Reuses the shared session ``sess_open`` (first
    open) + ``window.session['prev_close']``."""

    name = "gap_fill_state"
    input_cols = ("open", "high", "low", "close", "volume")
    feature_names = ("gap_fill_fraction", "gap_extended")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        acc = _update_session_cumulative(window)
        sess_open, close = acc["sess_open"], acc["close"]
        prev_close = window.session.get("prev_close")
        if prev_close is None:
            prev_close = np.full(window.n, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            denom = prev_close - sess_open
            ok = np.abs(denom) > 1e-9
            fill = np.where(ok, (close - sess_open) / denom, np.nan)
            extended = np.where(ok, (fill < 0).astype(np.float64), np.nan)
        return {"gap_fill_fraction": fill, "gap_extended": extended}


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
