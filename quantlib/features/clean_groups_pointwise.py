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


_QUARTER_END_MONTHS = (3, 6, 9, 12)


class CalendarEventsClean:
    """Point-in-time CALENDAR-event features from the minute's ET date (symbol-independent, broadcast):
    day_of_month_norm (day/31), week_of_month ((day−1)//7 + 1), is_opex_day (3rd Friday = Friday & day∈[15,21]),
    is_triple_witching (opex in a quarter-end month), is_quarter_end_month (Mar/Jun/Sep/Dec), is_first_week
    (day≤7), is_last_week (day≥22). Pure deterministic functions of window.minute_epoch in ET. Legacy:
    ``CalendarEventsGroup``."""

    name = "calendar_events"
    input_cols = ()
    feature_names = (
        "day_of_month_norm",
        "week_of_month",
        "is_opex_day",
        "is_triple_witching",
        "is_quarter_end_month",
        "is_first_week",
        "is_last_week",
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        et = dt.datetime.fromtimestamp(window.minute_epoch, _ET)
        day = et.day
        month = et.month
        is_friday = et.isoweekday() == 5  # ISO Mon=1..Sun=7 → Friday=5 (== polars weekday()==5)
        is_opex = is_friday and 15 <= day <= 21
        is_qend = month in _QUARTER_END_MONTHS
        vals = {
            "day_of_month_norm": float(day) / 31.0,
            "week_of_month": float((day - 1) // 7 + 1),
            "is_opex_day": 1.0 if is_opex else 0.0,
            "is_triple_witching": 1.0 if (is_opex and is_qend) else 0.0,
            "is_quarter_end_month": 1.0 if is_qend else 0.0,
            "is_first_week": 1.0 if day <= 7 else 0.0,
            "is_last_week": 1.0 if day >= 22 else 0.0,
        }
        return {name: np.full(n, value) for name, value in vals.items()}


_SECTORS: tuple[str, ...] = (
    "technology",
    "healthcare",
    "financial_services",
    "consumer_cyclical",
    "consumer_defensive",
    "industrials",
    "energy",
    "basic_materials",
    "real_estate",
    "utilities",
    "communication_services",
)


class SectorOneHotClean:
    """REFERENCE: one-hot of each symbol's GICS-aligned sector, broadcast across the day. sector_is_{sector}
    for the 11 canonical buckets + sector_is_unknown (unmapped). Reads the per-symbol normalized sector NAME
    from ``window.static['sector_name']`` (a static reference array, constant intraday). Legacy:
    ``SectorOneHotGroup``."""

    name = "sector"
    input_cols = ()
    feature_names = tuple(f"sector_is_{sector}" for sector in _SECTORS) + ("sector_is_unknown",)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        sector_name = window.static.get("sector_name")
        if sector_name is None:
            # no sector map → everything unknown (the legacy null-sector → unknown bucket).
            out: dict[str, np.ndarray] = {f"sector_is_{s}": np.zeros(n) for s in _SECTORS}
            out["sector_is_unknown"] = np.ones(n)
            return out
        names = np.asarray(sector_name)
        out = {f"sector_is_{s}": (names == s).astype(np.float64) for s in _SECTORS}
        # unknown = the symbol's sector is not one of the canonical buckets (unmapped/unclassified).
        is_known = np.isin(names, _SECTORS)
        out["sector_is_unknown"] = (~is_known).astype(np.float64)
        return out


_ASSET_FLAGS: tuple[tuple[str, str], ...] = (
    ("is_shortable", "shortable"),
    ("is_easy_to_borrow", "easy_to_borrow"),
    ("is_marginable", "marginable"),
    ("is_fractionable", "fractionable"),
)


class AssetFlagsClean:
    """REFERENCE: per-symbol tradability/borrow flags broadcast across the day — is_shortable,
    is_easy_to_borrow, is_marginable, is_fractionable. Reads each flag from ``window.static[<flag column>]``
    (per-symbol 0/1, constant intraday). NaN where the static flag is missing (sparse). Legacy:
    ``AssetFlagsGroup``."""

    name = "asset_flags"
    input_cols = ()
    feature_names = tuple(feature for feature, _ in _ASSET_FLAGS)

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        out: dict[str, np.ndarray] = {}
        for feature, column in _ASSET_FLAGS:
            flag = window.static.get(column)
            out[feature] = np.full(n, np.nan) if flag is None else np.asarray(flag, dtype=np.float64)
        return out
