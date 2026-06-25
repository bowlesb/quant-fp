"""Point-in-time feature groups ported to the ``CleanEngine`` interface.

These read only the CURRENT minute (the latest bar or the minute's timestamp) — no trailing window, no
cross-section, no carried state. They broadcast a per-symbol (or symbol-independent, e.g. calendar) value
computed from ``window.latest(col)`` / ``window.minute_epoch``. The simplest kind.

Time-based features use ``window.minute_epoch`` (the engine's point-in-time clock), NEVER wall-clock — so they
compute identically for historical backfill and live (the ctx.timestamp discipline).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from quantlib.features.clean_engine import Window

_ET = ZoneInfo("America/New_York")
_OPEN_MINUTE = 570  # 09:30 ET, minutes since ET midnight
_CLOSE_MINUTE = 960  # 16:00 ET


class CalendarClean:
    """Point-in-time CALENDAR features from the minute's ET timestamp (symbol-independent, broadcast to every
    symbol): minute_of_day_et, day_of_week (ISO Mon=1..Sun=7), minutes_since_open (vs 09:30 ET, negative
    pre-market), is_regular_session (1.0 in 09:30-16:00 ET). Uses window.minute_epoch — never wall-clock.
    Legacy: ``CalendarGroup``."""

    name = "calendar"
    input_cols = ()
    feature_names = ("minute_of_day_et", "day_of_week", "minutes_since_open", "is_regular_session")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        et = dt.datetime.fromtimestamp(window.minute_epoch, _ET)
        minute_of_day = float(et.hour * 60 + et.minute)
        day_of_week = float(et.isoweekday())
        minutes_since_open = minute_of_day - _OPEN_MINUTE
        is_regular = 1.0 if (_OPEN_MINUTE <= minute_of_day < _CLOSE_MINUTE) else 0.0
        return {
            "minute_of_day_et": np.full(n, minute_of_day),
            "day_of_week": np.full(n, day_of_week),
            "minutes_since_open": np.full(n, minutes_since_open),
            "is_regular_session": np.full(n, is_regular),
        }


_NEAR_DOLLAR_THRESHOLD = 0.02  # within 2 cents of a whole dollar


class RoundLevelsClean:
    """Point-in-time PRICE features from the minute's close: dist_to_round_dollar = |close − nearest whole
    dollar| (0..0.5); dist_to_half_dollar = |close − nearest x.00/x.50| (0..0.25); is_at_round_dollar = 1.0
    when within 2 cents of a whole dollar. Pure per-bar arithmetic on the latest close. Legacy:
    ``RoundLevelsGroup``."""

    name = "round_levels"
    input_cols = ("close",)
    feature_names = ("dist_to_round_dollar", "dist_to_half_dollar", "is_at_round_dollar")

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        close = window.latest("close")
        with np.errstate(invalid="ignore"):
            frac_dollar = close - np.floor(close)
            dist_dollar = np.minimum(frac_dollar, 1.0 - frac_dollar)
            half = close * 2.0
            frac_half = (half - np.floor(half)) / 2.0
            dist_half = np.minimum(frac_half, 0.5 - frac_half)
        return {
            "dist_to_round_dollar": dist_dollar,
            "dist_to_half_dollar": dist_half,
            "is_at_round_dollar": (dist_dollar < _NEAR_DOLLAR_THRESHOLD).astype(np.float64),
        }
