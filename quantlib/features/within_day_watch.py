"""Within-Day Parity Certifier — Phase-3 FIRST BUILD: the bus FRESHNESS TRIPWIRE.

Per docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §1. The subagent subscribes to its assigned group's LIVE
feature values on the feature-vector bus (``fv:<symbol>`` Redis Streams) and detects WHEN those values
change for a given (symbol, feature) — the signal that a hot-swap (or any compute change) has taken effect
live, so the agent knows to re-run the authoritative parity compare instead of polling blindly.

This is the bus-freshness half of ingredient 1: a bounded, by-NAME subscriber (``poll_views`` resolves the
frame's schema by fingerprint, so it reads the group's features regardless of producer set growth) that
tracks the last value seen per (symbol, feature) and emits a CHANGE event the minute a value differs.

The bus emit is gated ``FP_BUS=1`` in prod (default OFF). When the bus is unavailable this watch raises on
connect (the caller falls back to store-polling recent stream partitions — a slightly higher-latency
tripwire, designed for in §1 but not implemented here). Read-only: it consumes the bus, touches no store /
DB / live state. Bounded: subscribes to a SAMPLE of the group's symbols, never the universe.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.features.registry import REGISTRY


@dataclass
class FreshnessEvent:
    symbol: str
    feature: str
    minute: dt.datetime
    old_value: float
    new_value: float


def group_feature_names(group_name: str) -> list[str]:
    """The feature names declared by ``group_name`` (the columns the watch tracks for changes)."""
    return [spec.name for spec in REGISTRY.get_group(group_name).declare()]


def _changed(old: float, new: float) -> bool:
    """True if a tracked value changed meaningfully. Two NaNs are 'unchanged'; NaN↔finite IS a change."""
    old_nan, new_nan = (old != old), (new != new)  # NaN != NaN
    if old_nan and new_nan:
        return False
    if old_nan != new_nan:
        return True
    return not math.isclose(old, new, rel_tol=1e-12, abs_tol=1e-12)


class GroupFreshnessWatch:
    """Tracks the last bus value per (symbol, feature) for ONE group's sample symbols, emitting a
    FreshnessEvent the first time a value changes. The tripwire that confirms a swap took effect live."""

    def __init__(
        self,
        group_name: str,
        symbols: list[str],
        url: str = DEFAULT_REDIS_URL,
    ) -> None:
        self.group_name = group_name
        self.features = group_feature_names(group_name)
        self._consumer = BusConsumer(symbols, url=url)
        self._last: dict[tuple[str, str], float] = {}

    def poll_once(self, block_ms: int = 1000, count: int = 200) -> list[FreshnessEvent]:
        """Read the next batch of live frames for the sample symbols; return any (symbol, feature) whose
        value CHANGED vs the last seen. The first observation of a (symbol, feature) seeds the baseline
        (no event); subsequent changes emit. Bounded by ``count`` frames per call."""
        events: list[FreshnessEvent] = []
        for view in self._consumer.poll_views(block_ms=block_ms, count=count):
            for feature in self.features:
                new_value = view.get(feature)
                key = (view.symbol, feature)
                if key not in self._last:
                    self._last[key] = new_value
                    continue
                old_value = self._last[key]
                if _changed(old_value, new_value):
                    events.append(FreshnessEvent(view.symbol, feature, view.minute, old_value, new_value))
                    self._last[key] = new_value
        return events

    def ping(self) -> bool:
        return self._consumer.ping()
