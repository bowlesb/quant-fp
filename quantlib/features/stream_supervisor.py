"""Self-healing supervisor around ``StockDataStream.run()`` for the live capture reader.

ROOT CAUSE this addresses (DataIntegrity 2026-06-19/20). ``StockDataStream.run()`` (alpaca-py) runs
``_run_forever``, which is robust to ordinary websocket drops (it reconnects in-loop). But it RETURNS
cleanly — ending the reader process — on a reconnect whose re-auth/re-subscribe is rejected with a
``ValueError`` containing "insufficient subscription". At the volatile market open a websocket drop triggers
a reconnect that re-auths + re-subscribes the full universe; if the prior connection has not yet been torn
down server-side, Alpaca's single-connection-per-account rejects it -> clean ``run()`` return -> the reader
process exits. Observed live: fc exited 06-17T13:21Z + 13:45Z (across the open) and 06-19T10:06Z. The
``ops/live_monitor.sh`` cron then ``docker start``s it on its next <=3-minute poll, so capture is restored
but a gap of up to ~3 minutes opens — worst exactly at the open.

This supervisor closes that gap IN-PROCESS: when ``stream.run()`` returns while the session is still active
(i.e. NOT an intentional bounded stop), it rebuilds a fresh stream, re-subscribes, and re-enters ``run()``
after a short backoff. The backoff is the actual root-cause fix — it lets Alpaca free the prior connection
before the re-auth, so the re-subscribe is accepted; the re-enter is the self-heal. It is gated on a
session-active wall-clock so it NEVER loops after the extended-session close, and it caps CONSECUTIVE
failures so an unrecoverable error (e.g. bad creds) cannot spin forever. The ``ops/live_monitor.sh``
docker-start remains the backstop if this supervisor itself ever exits.

DESIGN INVARIANTS (the supervisor must never be worse than the bare ``run()`` it wraps):
  * An intentional bounded stop (``stopped_intentionally`` True — the ``max_minutes`` sentinel path) returns
    immediately, NO re-enter. The benchmark/sim bounded-exit path is byte-unchanged.
  * Post-session-close, it returns instead of reconnecting (no infinite reconnect after 20:00 ET).
  * A build/subscribe/run exception is caught, logged, and counted as a failed attempt — it is NEVER
    propagated (a supervisor that crashes defeats its purpose); but ``max_attempts`` CONSECUTIVE failures
    end it cleanly so a hard failure surfaces to the monitor backstop rather than spinning.
  * A run that lasted longer than ``healthy_run_seconds`` RESETS the consecutive-failure counter — a single
    long-lived session that later drops should get the full retry budget again, not be one strike from the cap.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from quantlib.features.session import ET_ZONE, EXT_CLOSE_MINUTE, WARMUP_START_MINUTE

logger = logging.getLogger("stream_supervisor")

DEFAULT_MAX_ATTEMPTS = 12  # consecutive failed (re)connects before giving up to the monitor backstop
DEFAULT_BASE_BACKOFF_S = 2.0  # first reconnect wait — >= ~1-2s so Alpaca frees the prior connection
DEFAULT_MAX_BACKOFF_S = 30.0  # cap the exponential backoff
DEFAULT_HEALTHY_RUN_S = 60.0  # a run() that lasted at least this long counts as healthy -> reset the budget
SESSION_GRACE_MINUTES = 10  # keep supervising this many minutes past the extended close before standing down


class SupervisableStream(Protocol):
    """The slice of ``StockDataStream`` the supervisor drives. ``run()`` blocks until the stream stops."""

    def run(self) -> None:
        ...


def session_active_now(day: str, *, now_utc: dt.datetime | None = None) -> bool:
    """True if wall-clock time is within the extended capture session for ``day`` (08:00-20:00 ET, plus a
    small grace), so a dropped stream is worth reconnecting. After the extended close (+grace) the session
    is over and the supervisor stands down rather than reconnecting into a dead session.

    ``day`` is the session date (YYYY-MM-DD); the window is anchored in ET and compared in UTC so it is
    DST-correct. ``now_utc`` is injectable for tests (default: real wall clock)."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    date = dt.date.fromisoformat(day)
    et = ZoneInfo(ET_ZONE)
    start_et = dt.datetime.combine(
        date, dt.time(WARMUP_START_MINUTE // 60, WARMUP_START_MINUTE % 60), tzinfo=et
    )
    end_minute = EXT_CLOSE_MINUTE + SESSION_GRACE_MINUTES
    end_et = dt.datetime.combine(date, dt.time(end_minute // 60, end_minute % 60), tzinfo=et)
    start_utc = start_et.astimezone(dt.timezone.utc)
    end_utc = end_et.astimezone(dt.timezone.utc)
    return start_utc <= now < end_utc


def run_stream_supervised(
    build_and_subscribe: Callable[[], SupervisableStream],
    *,
    day: str | None,
    stopped_intentionally: Callable[[], bool],
    session_active: Callable[[str], bool] = session_active_now,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_backoff_s: float = DEFAULT_BASE_BACKOFF_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    healthy_run_s: float = DEFAULT_HEALTHY_RUN_S,
) -> None:
    """Run a fresh ``StockDataStream`` to completion, self-healing across unexpected ``run()`` returns.

    ``build_and_subscribe`` MUST construct a NEW stream, attach its bar/tick subscriptions, and return it —
    it is called once per (re)connect because a stream is single-use once ``run()`` has returned. ``day`` is
    the session date for the session-active gate (if None, the gate is skipped — single un-bounded run, as
    for ad-hoc/benchmark use without a session clock). ``stopped_intentionally`` is checked after each
    ``run()`` returns: when True (the ``max_minutes`` bounded-stop path), the supervisor returns immediately
    with NO re-enter, so the bounded/benchmark exit is byte-unchanged.
    """
    attempt = 0
    while True:
        run_started = time.monotonic()
        try:
            stream = build_and_subscribe()
            stream.run()
        except Exception as error:  # noqa: BLE001 — a supervisor MUST NOT crash; log + count as a failure
            logger.error("[supervisor] stream.run() raised (%s) — treating as a failed attempt", error)
        else:
            if stopped_intentionally():
                logger.info("[supervisor] stream stopped intentionally (bounded max_minutes) — done")
                return

        if stopped_intentionally():  # set during run() even if run() then raised on teardown
            logger.info("[supervisor] intentional stop observed — done")
            return

        if day is not None and not session_active(day):
            logger.info(
                "[supervisor] stream returned after the %s session close — standing down (no reconnect)", day
            )
            return

        # A run that lived long enough was healthy; a fresh drop deserves the full retry budget, not the cap.
        if time.monotonic() - run_started >= healthy_run_s:
            attempt = 0

        attempt += 1
        if attempt > max_attempts:
            logger.error(
                "[supervisor] %d consecutive reconnect attempts failed — standing down to the monitor "
                "backstop (it will docker-start the reader)",
                max_attempts,
            )
            return

        backoff = min(base_backoff_s * (2 ** (attempt - 1)), max_backoff_s)
        logger.warning(
            "[supervisor] stream.run() returned unexpectedly while the session is active — reconnect "
            "attempt %d/%d after %.1fs backoff (lets Alpaca free the prior connection before re-auth)",
            attempt,
            max_attempts,
            backoff,
        )
        time.sleep(backoff)
