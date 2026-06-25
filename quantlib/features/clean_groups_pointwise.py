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
