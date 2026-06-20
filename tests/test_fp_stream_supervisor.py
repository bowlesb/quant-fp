"""Fault-injection tests for the live-capture stream supervisor (quantlib.features.stream_supervisor).

The supervisor wraps alpaca's ``StockDataStream.run()`` so that a clean ``run()`` return mid-session (the
observed fc mid-session exit: a reconnect tripping Alpaca's single-connection/subscription limit) self-heals
in-process instead of ending the reader and waiting up to ~3 min for the monitor docker-start. The supervisor
is only as trustworthy as this test, so we INJECT each fault and assert the recovery + every guardrail:

  * an unexpected mid-session run() return -> rebuild + re-subscribe + re-enter (the self-heal);
  * an intentional bounded stop (max_minutes) -> return immediately, NO re-enter (benchmark/sim unchanged);
  * a run() return AFTER the session close -> stand down, no reconnect (no infinite post-close loop);
  * a long healthy run that then drops -> the consecutive-failure budget RESETS (full retries again);
  * unrecoverable repeated failures -> stand down at max_attempts (to the monitor backstop), never spin;
  * an exception raised inside run() -> caught + counted, NEVER propagated (a crashing supervisor is worse
    than the bare run() it wraps).

Backoff is disabled (base/max = 0) and the session clock is injected — no real time passes and no real
socket is opened.
"""
from __future__ import annotations

import datetime as dt

from quantlib.features import stream_supervisor
from quantlib.features.stream_supervisor import run_stream_supervised, session_active_now


class ScriptedStream:
    """A fake StockDataStream whose run() pops the next scripted behavior each (re)connect.

    Each script entry is one of: "return" (run() returns cleanly — the unexpected/clean-exit case),
    "stop" (run() returns having set the intentional-stop flag — the bounded max_minutes case), or
    a callable raising an exception (the run()-raised case). ``builds`` counts (re)subscriptions so the
    test can assert the supervisor rebuilt a fresh subscribed stream per attempt.
    """

    def __init__(self, script: list, stop_flag: dict) -> None:
        self._script = list(script)
        self._stop_flag = stop_flag
        self.runs = 0
        self.subscribed = 0

    def subscribe(self) -> None:
        self.subscribed += 1

    def run(self) -> None:
        self.runs += 1
        behavior = self._script.pop(0)
        if behavior == "stop":
            self._stop_flag["stopped"] = True
            return
        if behavior == "return":
            return
        if callable(behavior):
            behavior()  # raise an injected exception
            return
        raise AssertionError(f"unknown scripted behavior {behavior!r}")


def _harness(script: list, *, session_active: bool = True, **kwargs):
    """Run the supervisor over ``script`` (one behavior per attempt) with backoff disabled and an injected
    session clock — no real time passes, no real socket opens. Returns (builds, stop_flag)."""
    stop_flag = {"stopped": False}
    builds: list[int] = []
    per_attempt = list(script)

    def build_and_subscribe_seq() -> ScriptedStream:
        idx = len(builds)
        builds.append(1)
        stream = ScriptedStream([per_attempt[idx]], stop_flag)
        stream.subscribe()
        return stream

    run_stream_supervised(
        build_and_subscribe_seq,
        day="2026-06-22",
        stopped_intentionally=lambda: bool(stop_flag["stopped"]),
        session_active=lambda day: session_active,
        base_backoff_s=0.0,
        max_backoff_s=0.0,
        healthy_run_s=kwargs.get("healthy_run_s", 60.0),
        max_attempts=kwargs.get("max_attempts", stream_supervisor.DEFAULT_MAX_ATTEMPTS),
    )
    return builds, stop_flag


def test_unexpected_return_midsession_reconnects_then_stops() -> None:
    # First run() returns unexpectedly (the insufficient-subscription clean exit) -> supervisor reconnects;
    # the SECOND attempt is an intentional stop -> supervisor returns. 2 builds = 1 self-heal.
    builds, stop_flag = _harness(["return", "stop"], session_active=True)
    assert len(builds) == 2  # rebuilt + re-subscribed once after the unexpected return
    assert stop_flag["stopped"] is True


def test_intentional_bounded_stop_never_reconnects() -> None:
    # The bounded max_minutes path: run() returns having set the stop flag -> NO re-enter (1 build only).
    builds, stop_flag = _harness(["stop", "return", "return"], session_active=True)
    assert len(builds) == 1  # byte-equivalent to the bare bounded run() — no supervision side effects
    assert stop_flag["stopped"] is True


def test_return_after_session_close_stands_down() -> None:
    # run() returns but the session is over -> stand down immediately, NO reconnect (no post-close loop).
    builds, _stop = _harness(["return", "return", "return"], session_active=False)
    assert len(builds) == 1


def test_max_attempts_cap_stands_down_to_backstop() -> None:
    # Unrecoverable: every run() returns unexpectedly while active -> cap at max_attempts, never spin.
    script = ["return"] * 10
    builds, _stop = _harness(script, session_active=True, max_attempts=3)
    # 1 initial + 3 reconnect attempts = 4 builds, then it stands down (does NOT consume all 10).
    assert len(builds) == 4


def test_run_exception_is_caught_and_counted_not_propagated() -> None:
    def boom() -> None:
        raise RuntimeError("injected websocket auth blowup")

    # run() raising must NOT propagate; it counts as a failed attempt and is retried under the cap.
    script = [boom, boom, "stop"]
    builds, stop_flag = _harness(script, session_active=True, max_attempts=5)
    assert len(builds) == 3  # two raised attempts + the stop, no exception escaped
    assert stop_flag["stopped"] is True


def test_healthy_run_resets_failure_budget() -> None:
    # A run() that lasts >= healthy_run_s resets the consecutive-failure counter, so a later drop gets the
    # full budget again. With max_attempts=1: a healthy long run, then ONE unexpected return must still be
    # allowed exactly one reconnect (not be pre-counted toward the cap). healthy_run_s=0 makes EVERY run
    # count as healthy, so each unexpected return resets attempt->0 then increments to 1 (== cap) and we get
    # a reconnect every time until the stop.
    builds, stop_flag = _harness(
        ["return", "return", "return", "stop"], session_active=True, max_attempts=1, healthy_run_s=0.0
    )
    assert len(builds) == 4  # each healthy unexpected return is granted its reconnect; cap never trips
    assert stop_flag["stopped"] is True


def test_session_active_now_window() -> None:
    # 08:00-20:00 ET (+10m grace) on the session day, DST-correct. 2026-06-22 is EDT (UTC-4).
    day = "2026-06-22"
    before = dt.datetime(2026, 6, 22, 11, 0, tzinfo=dt.timezone.utc)  # 07:00 ET — before the 08:00 anchor
    during = dt.datetime(2026, 6, 22, 17, 0, tzinfo=dt.timezone.utc)  # 13:00 ET — mid-session
    after = dt.datetime(2026, 6, 23, 0, 30, tzinfo=dt.timezone.utc)  # 20:30 ET — past close + grace
    assert session_active_now(day, now_utc=before) is False
    assert session_active_now(day, now_utc=during) is True
    assert session_active_now(day, now_utc=after) is False
