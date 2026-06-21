"""Watchdog regression: a hung profile_sim run MUST self-abort at its wall-clock cap (never leak a core).

``run_profile_sim`` drives a multiprocess sim (mock + N shard workers) whose reader only stops once it has
dispatched ``max_minutes`` bar-minutes. If the mock cannot serve — e.g. its port is already bound — the
reader's bare ``stream.run()`` re-enters alpaca-py's internal reconnect loop and NEVER returns, burning a
whole CPU core indefinitely (observed live: orphaned profile_sim procs running 2.5-3h). This asserts the
hard wall-clock cap (FP_PROFILE_SIM_TIMEOUT_S) fires and raises TimeoutError instead, AND that no mock /
shard-worker child outlives the cap. It is light + always-on (the heavy happy-path e2e gate stays opt-in).
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from quantlib.features.bench_stream import PORT
from quantlib.features.profile_sim import _sim_timeout_s, run_profile_sim


def _live_python_children(parent_pid: int) -> list[int]:
    """Live (non-zombie) python processes whose ppid is NOT this test process — i.e. sim mock/worker
    descendants that re-parented to init after a leak. Reads /proc directly (no ps in the image)."""
    leaked: list[int] = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            head, rest = stat_path.read_text().split(")", 1)
            comm = head.split("(", 1)[1]
            state, ppid = rest.split()[0], rest.split()[1]
        except (FileNotFoundError, ProcessLookupError, IndexError):
            continue
        pid = int(stat_path.parent.name)
        if "python" in comm and state != "Z" and ppid != str(parent_pid) and pid != parent_pid:
            leaked.append(pid)
    return leaked


def test_sim_timeout_s_honours_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_PROFILE_SIM_TIMEOUT_S", "7.5")
    assert _sim_timeout_s(measure=10) == 7.5


def test_sim_timeout_s_scales_with_measured_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FP_PROFILE_SIM_TIMEOUT_S", raising=False)
    # A short run gets the floor; a long run gets per-minute headroom above the floor (never less).
    assert _sim_timeout_s(measure=1) >= _sim_timeout_s(measure=0)
    assert _sim_timeout_s(measure=10_000) > _sim_timeout_s(measure=1)


def test_hung_run_self_aborts_at_cap_and_leaves_no_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Force the hang: occupy the mock's port so the in-sim mock cannot bind -> the reader spins on the
    reconnect loop forever. The watchdog must (a) raise TimeoutError at the (tiny) cap, not hang the test,
    and (b) leave NO new live python descendant (the mock + shard workers must be reaped, not leaked)."""
    monkeypatch.setenv("FP_PROFILE_SIM_TIMEOUT_S", "8")
    monkeypatch.setenv("ALPACA_KEY_ID", "mock")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "mock")

    self_pid = os.getpid()
    before = set(_live_python_children(self_pid))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", PORT))
    blocker.listen(1)
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            run_profile_sim(
                n_symbols=8, n_shards=2, measure=3, warmup=1, window=60, root=str(tmp_path / "hung_store")
            )
        elapsed = time.monotonic() - start
        # Aborted at ~the cap (8s), not the multi-hour hang it would otherwise be. Generous upper bound
        # covers the SIGKILL teardown of the child group under host load.
        assert elapsed < 60, f"watchdog did not abort near the cap (took {elapsed:.0f}s)"

        time.sleep(2)  # let the kernel reap the SIGKILLed group
        leaked = set(_live_python_children(self_pid)) - before
        assert not leaked, f"hung sim leaked live python descendants (mock/workers not reaped): {leaked}"
    finally:
        blocker.close()
