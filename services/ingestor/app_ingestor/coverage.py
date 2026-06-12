"""Live coverage invariant: streamed == subscribed, alarmed.

Built in from day one, not bolted on. Per shard, each minute we know which of the
shard's EXPECTED (subscribed) symbols actually produced a trade. A subscribed liquid
name that goes silent for too long during RTH is the capture-regression signal qa
watches — it means the at-scale stream is dropping a name we believe we're capturing,
which would otherwise only surface in a backfill diff days later.

We measure coverage at BAR boundaries: a bar arrives for every universe symbol each
minute (the minute-close signal), so when a shard symbol's bar arrives we check
whether that minute also had a trade. Bars are the ground truth for "this minute
happened"; trades are what we must not be silently dropping.

Genuinely no-trade minutes for illiquid names are real, not a regression — so the
alarm is on CONSECUTIVE silent RTH minutes per symbol (default >K), not on any single
empty minute. The Prometheus gauges expose the live streamed/subscribed ratio and the
worst per-symbol silent streak so the regression is visible immediately.
"""
import logging
import os

from prometheus_client import Counter, Gauge, start_http_server

logger = logging.getLogger("ingestor.coverage")

# Consecutive silent RTH minutes before a subscribed name is considered dropped.
SILENCE_ALARM_MINUTES = int(os.environ.get("COVERAGE_SILENCE_ALARM_MIN", "10"))

_coverage_ratio = Gauge(
    "ingestor_shard_coverage_ratio",
    "Fraction of this shard's subscribed symbols that traded in the last closed minute",
    ["shard"],
)
_silent_symbols = Gauge(
    "ingestor_shard_silent_symbols",
    "Count of subscribed symbols silent beyond the alarm threshold this minute",
    ["shard"],
)
_max_silent_streak = Gauge(
    "ingestor_shard_max_silent_streak",
    "Longest current per-symbol consecutive-silent-minute streak in this shard",
    ["shard"],
)
_dropped_alarms = Counter(
    "ingestor_shard_dropped_alarms_total",
    "Total times a subscribed symbol crossed the silence alarm threshold",
    ["shard"],
)


class ShardCoverage:
    """Tracks per-symbol trade coverage for one shard and emits Prometheus gauges.

    `record_bar` is called when a symbol's minute-close bar arrives, with whether
    that minute had a trade. We accumulate a minute's worth of bar arrivals, then
    emit the shard's coverage when the minute rolls over.
    """

    def __init__(self, shard_id: int, expected: set[str], metrics_port: int) -> None:
        self.shard_id = shard_id
        self.expected = expected
        self.silent_streak: dict[str, int] = {symbol: 0 for symbol in expected}
        self._current_minute: int | None = None
        self._traded_this_minute: set[str] = set()
        self._seen_this_minute: set[str] = set()
        start_http_server(metrics_port)

    def record_bar(self, symbol: str, minute_epoch: int, had_trade: bool) -> None:
        if symbol not in self.expected:
            return
        if self._current_minute is None:
            self._current_minute = minute_epoch
        elif minute_epoch != self._current_minute:
            self._emit_minute()
            self._current_minute = minute_epoch
            self._traded_this_minute = set()
            self._seen_this_minute = set()
        self._seen_this_minute.add(symbol)
        if had_trade:
            self._traded_this_minute.add(symbol)

    def maybe_emit(self) -> None:
        """No-op hook for the worker's idle path; the minute roll in record_bar is
        what actually emits. Kept so the worker can call it without knowing the
        internals (and so a future wall-clock flush has a home)."""
        return

    def _emit_minute(self) -> None:
        if not self._seen_this_minute:
            return
        for symbol in self.expected:
            if symbol in self._traded_this_minute:
                self.silent_streak[symbol] = 0
            elif symbol in self._seen_this_minute:
                self.silent_streak[symbol] += 1

        ratio = len(self._traded_this_minute) / max(len(self.expected), 1)
        over_threshold = [
            symbol
            for symbol, streak in self.silent_streak.items()
            if streak >= SILENCE_ALARM_MINUTES
        ]
        worst = max(self.silent_streak.values(), default=0)

        _coverage_ratio.labels(shard=str(self.shard_id)).set(ratio)
        _silent_symbols.labels(shard=str(self.shard_id)).set(len(over_threshold))
        _max_silent_streak.labels(shard=str(self.shard_id)).set(worst)

        for symbol in over_threshold:
            if self.silent_streak[symbol] == SILENCE_ALARM_MINUTES:
                _dropped_alarms.labels(shard=str(self.shard_id)).inc()
                logger.warning(
                    "COVERAGE ALARM shard=%d symbol=%s silent %d consecutive RTH minutes "
                    "(subscribed liquid name dropped?)",
                    self.shard_id, symbol, self.silent_streak[symbol],
                )
