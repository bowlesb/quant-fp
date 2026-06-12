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

TWO worker-level failure modes the per-symbol silence alarm CANNOT see, so they get
their own first-class signals (the "10-vs-50" lesson: a healthy reader subscribed to
50 while a wedged worker aggregates only 10, and nothing screams):
  - **A wedged worker** stops calling record_bar entirely, so _emit_minute never
    fires and no per-symbol streak ever increments. The HEARTBEAT gauge
    (`..._worker_heartbeat_unixtime`, bumped every loop iteration incl. the idle
    path) goes stale -> Prometheus alarms on `time() - heartbeat > K`, independent of
    record_bar. This is the observed-set check: liveness of the AGGREGATION, not just
    of subscriptions.
  - **A reader outpacing a worker** shows as growing `..._shard_queue_depth` — the
    backpressure that precedes drops, made measurable before any tick is lost.
A respawned worker also resets its silent-streak/heartbeat state cleanly; the restart
gap is visible as a coverage dip + a one-trade tick-rule cold start (self-healing, the
same as any process boot).
"""
import logging
import os
import time

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
# Worker liveness: a wedged worker (healthy reader, stuck aggregation) shows as a
# STALE heartbeat — Prometheus alarms on time() - heartbeat, no per-symbol signal
# needed. This is what catches the 10-vs-50 case the silence alarm is blind to.
_worker_heartbeat = Gauge(
    "ingestor_worker_heartbeat_unixtime",
    "Unix time of this worker's last processing-loop iteration (stale => wedged)",
    ["shard"],
)
# Reader->worker backpressure: a worker falling behind shows as rising depth BEFORE
# any tick is dropped (mp queue is bounded, so this also bounds memory).
_queue_depth = Gauge(
    "ingestor_shard_queue_depth",
    "Approximate reader->worker queue depth for this shard (rising => worker behind)",
    ["shard"],
)
# This shard's subscribed OFI-symbol count. sum() across shards = the live OFI set
# size; a Prometheus alert on the sum dropping below the M2 floor (500) backs up the
# build-time assert with a RUNNING-system signal (the assert only fires at startup).
_shard_expected_symbols = Gauge(
    "ingestor_shard_expected_symbols",
    "Count of OFI symbols this shard is subscribed to (sum across shards = OFI set size)",
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
        _shard_expected_symbols.labels(shard=str(shard_id)).set(len(expected))

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

    def heartbeat(self) -> None:
        """Bump the worker-liveness gauge. Called every processing-loop iteration —
        INCLUDING the idle (queue-empty) path — so a wedged worker is detectable as a
        stale heartbeat even when no bars arrive to drive the per-symbol coverage."""
        _worker_heartbeat.labels(shard=str(self.shard_id)).set(time.time())

    def set_queue_depth(self, depth: int) -> None:
        """Publish the reader->worker queue depth so backpressure (a worker falling
        behind the reader) is visible before any tick is dropped."""
        _queue_depth.labels(shard=str(self.shard_id)).set(depth)

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
