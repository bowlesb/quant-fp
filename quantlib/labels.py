"""Label construction: forward cross-sectional excess returns.

A label for (symbol, ts) is the symbol's forward return over a horizon MINUS the
universe's cross-sectional median forward return over the same window — i.e. how
much it out/under-performs its peers. That is exactly what the cross-sectional
ranking model predicts.

Labels legitimately use data AFTER ts (that's what a forward return is). The
no-leakage rule applies to FEATURES (which must not see the future); labels live
in a separate table and are never fed back as inputs. Pure functions here; I/O in
the backfiller.
"""
import math
import statistics
from datetime import date, datetime, timedelta

LABEL_HORIZONS = [30, 60]        # minutes
OVERNIGHT_HORIZON = "overnight"  # close -> next session open


def overnight_return_series(
    daily_open: dict[date, float], daily_close: dict[date, float]
) -> dict[date, float]:
    """Per-day overnight simple return = next trading day's OPEN / this day's CLOSE - 1.
    Keyed by the day held (d); the label realizes at d+1's open. NaN where undefined."""
    days = sorted(set(daily_open) & set(daily_close))
    out: dict[date, float] = {}
    for i in range(len(days) - 1):
        day, next_day = days[i], days[i + 1]
        close = daily_close[day]
        out[day] = (daily_open[next_day] / close - 1.0) if close else math.nan
    return out


def horizon_name(horizon_minutes: int) -> str:
    return f"fwd_{horizon_minutes}m"


def forward_return_series(
    close_by_ts: dict[datetime, float], horizon_minutes: int
) -> dict[datetime, float]:
    """Forward simple return at each ts using the bar exactly horizon minutes later
    (looked up by timestamp, so gaps don't silently shorten the horizon). NaN where
    the forward bar or a valid base price is missing."""
    out: dict[datetime, float] = {}
    for ts, close in close_by_ts.items():
        target = close_by_ts.get(ts + timedelta(minutes=horizon_minutes))
        out[ts] = (target / close - 1.0) if (target is not None and close) else math.nan
    return out


def cross_sectional_excess(returns: dict[str, float]) -> dict[str, float]:
    """Subtract the cross-sectional median (over the non-NaN members) from each
    member's return. Median is robust to outliers. NaN inputs stay NaN."""
    valid = [value for value in returns.values() if not math.isnan(value)]
    if not valid:
        return {symbol: math.nan for symbol in returns}
    median = statistics.median(valid)
    return {
        symbol: (value - median if not math.isnan(value) else math.nan)
        for symbol, value in returns.items()
    }
