"""`StaleEntryTracker` — a bounded detector that decides when an unfilled entry order is GENUINELY dead.

The problem it fixes: a bet whose entry order never landed at the broker (it returns Alpaca's
order-not-found, code 40410000) is never filled and never fillable, but the manage loop kept re-querying
the broker for it every ~1.4s tick forever — a ~4 GET/sec spin per stale bet, shared across all three
strategies' reconcile/manage logic. With all 3 cut over + a Monday reopen, the combined stale-bet GET load
could approach Alpaca's per-minute limit and starve legitimate calls.

The fix must be PRECISE (the Lead's care note): expire a bet ONLY on a GENUINE not-found, never on a
transient API miss or a momentary 404 race against a real in-flight order. So this tracker requires N
CONSECUTIVE not-found checks spanning at least M seconds before it declares an entry terminal — a single
404 (or a transient error that resets the streak) can never expire a live order. Any non-not-found outcome
(the order appears, or a transient error) resets the streak.

State is in-memory per (strategy process) and per coid; it doesn't need to be durable — a restart simply
re-observes the not-found streak from zero, which is safe (it just delays the terminal decision by N
checks after a restart, never expiring a live order prematurely).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from alpaca.common.exceptions import APIError

# Alpaca's "order not found for <coid>" error code — a GENUINE not-found (the order never existed / was
# purged), distinct from a transient API failure.
ORDER_NOT_FOUND_CODE = 40410000


def is_order_not_found(exc: APIError) -> bool:
    """True iff this APIError is Alpaca's order-not-found (code 40410000) — the genuine "the broker has no
    such order" signal, NOT a transient 5xx/timeout. Only this feeds the stale-entry tracker."""
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            return int(code) == ORDER_NOT_FOUND_CODE
        except (TypeError, ValueError):
            return False
    return str(ORDER_NOT_FOUND_CODE) in str(exc)


# Defaults: an entry is declared dead only after 5 consecutive genuine not-founds spanning >= 30s. At the
# ~1.4s manage cadence that is ~5-20 ticks, comfortably past any transient 404 / in-flight-order race.
DEFAULT_MIN_CHECKS = 5
DEFAULT_MIN_SECONDS = 30.0


@dataclass
class _Streak:
    count: int
    first_seen: dt.datetime
    last_seen: dt.datetime


class StaleEntryTracker:
    """Per-coid consecutive-not-found streak tracker. Declares an entry terminal only after a bounded
    streak (N consecutive not-founds over >= M seconds), so a transient/race 404 never expires it."""

    def __init__(
        self, *, min_checks: int = DEFAULT_MIN_CHECKS, min_seconds: float = DEFAULT_MIN_SECONDS
    ) -> None:
        self._min_checks = min_checks
        self._min_seconds = min_seconds
        self._streaks: dict[str, _Streak] = {}

    def record_not_found(self, coid: str, now: dt.datetime) -> bool:
        """Record a GENUINE order-not-found for ``coid`` at ``now``. Returns True iff the entry is now
        declared TERMINAL (>= min_checks consecutive not-founds spanning >= min_seconds). The caller passes
        only genuine 40410000 not-founds here; transient errors / a found order call ``reset`` instead."""
        streak = self._streaks.get(coid)
        if streak is None:
            self._streaks[coid] = _Streak(count=1, first_seen=now, last_seen=now)
            return False
        streak.count += 1
        streak.last_seen = now
        spanned = (streak.last_seen - streak.first_seen).total_seconds()
        return streak.count >= self._min_checks and spanned >= self._min_seconds

    def reset(self, coid: str) -> None:
        """Clear ``coid``'s streak — call on ANY non-not-found outcome (the order appeared, or a transient
        error), so only a CONSECUTIVE genuine-not-found run can ever reach the terminal threshold."""
        self._streaks.pop(coid, None)

    def forget(self, coid: str) -> None:
        """Drop a coid's tracking once its bet is resolved (terminal/closed) — keeps the map bounded."""
        self._streaks.pop(coid, None)

    def streak_count(self, coid: str) -> int:
        """The current consecutive not-found count for a coid (0 if none) — for logging / tests."""
        streak = self._streaks.get(coid)
        return streak.count if streak is not None else 0
