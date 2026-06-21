"""Pre-flight latency + per-group profiler — fake-run production through the streaming sim and report the
two things a person needs before the open:

  1. END-TO-END bar->vector latency (p50/p95/p99). For each minute, the universe-wide feature vector for
     that minute is not READY until the SLOWEST shard has assembled its slice, so the bet-relevant latency
     is ``max_over_shards(vector-ready wall) - reader-dispatch wall`` per minute (the LAST-bar anchor, the
     same one real_capture's ``feature_assemble_seconds`` uses — it excludes the post-bet write). This is
     the number that must fit the ~100ms budget as bars flow in.

  2. PER-GROUP compute ranking. The phase decomposition in ``stream_sim._report`` buckets ~82 at-T groups
     into a single "rest" number, which hides WHICH group is slow. With ``FP_SIM_GROUP_TIMINGS=1`` each
     non-reduction group's own ``compute_latest`` ms is logged per minute; here we rank groups by their
     p50/p99 so the slowest one is named, not hidden in an aggregate. (Reduction groups share one batched
     marshal, so their per-group share is the reduction-emit phase split evenly — reported for context.)

Runnable before each open as a pre-flight check:

    docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \
        python -m quantlib.features.profile_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]

It REUSES the exact sim machinery (the real StockDataStream against the msgpack mock, the same shard
workers and incremental fast path) — it only flips the profiler's two logging switches on and prints a
profile-oriented report. See docs/PROFILE_SIM.md for how to read the output.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from quantlib.features.bench_stream import (
    PORT,
    SESSION_DAY,
    _start_mock,
    synth_daily,
    synth_reference,
    synth_symbols,
)
from quantlib.features.capture import DEFAULT_BUFFER_MINUTES
from quantlib.features.stream_sim import _percentile, run_streaming_sim

BUDGET_MS = 100.0  # the bar->vector budget (CLAUDE memory: ~100ms hard ceiling as bars flow in)

# Hard wall-clock cap for ONE sim run. The sim is a multiprocess loop (mock + N shard workers) whose reader
# only stops once it has dispatched ``max_minutes`` bar-minutes; if the mock dies / fails to bind / drops the
# socket early (e.g. a port collision), the reader's bare ``stream.run()`` re-enters alpaca-py's internal
# reconnect loop and NEVER returns, leaking whole CPU cores for hours. This watchdog GUARANTEES the run (and
# every mock/worker child it spawned) is torn down at the cap. Override per run via FP_PROFILE_SIM_TIMEOUT_S.
DEFAULT_TIMEOUT_S = 600.0  # generous: a healthy ref-scale run is ~10-15s; full 1000/16 is well under a minute
# Per-measured-minute headroom so a deliberately large [minutes] run still gets a fair cap, never less than
# the floor — the cap should bound a HANG, not clip a legitimately long (but progressing) run.
_TIMEOUT_PER_MINUTE_S = 30.0


def _read_shard_records(bench: Path) -> dict[str, list[dict]]:
    """Group every shard's per-minute bench record by minute (the per-shard JSONL the workers wrote)."""
    by_minute: dict[str, list[dict]] = defaultdict(list)
    for shard_file in sorted(bench.glob("shard-*.jsonl")):
        for line in shard_file.read_text().splitlines():
            record = json.loads(line)
            by_minute[record["minute"]].append(record)
    return by_minute


def _read_dispatch_walls(bench: Path) -> dict[str, float]:
    """The reader's ``dispatch_wall`` per minute (the last-bar-in-hand cross-process wall-clock anchor)."""
    reader_file = bench / "reader.jsonl"
    if not reader_file.exists():
        return {}
    walls: dict[str, float] = {}
    for line in reader_file.read_text().splitlines():
        record = json.loads(line)
        if "dispatch_wall" in record:
            walls[record["minute"]] = record["dispatch_wall"]
    return walls


def end_to_end_latencies_ms(
    by_minute: dict[str, list[dict]], dispatch_walls: dict[str, float], warmup: int
) -> list[float]:
    """Per minute, the universe vector is ready when the SLOWEST shard finished: ``max(ready_wall) -
    dispatch_wall``, in ms, over the steady-state (post-warmup) minutes that have both stamps."""
    minutes_sorted = sorted(by_minute)
    latencies: list[float] = []
    for minute in minutes_sorted:
        if minute not in dispatch_walls:
            continue
        readies = [rec["ready_wall"] for rec in by_minute[minute] if "ready_wall" in rec]
        if not readies:
            continue
        latencies.append((max(readies) - dispatch_walls[minute]) * 1000.0)
    return latencies[warmup:]


def group_samples(by_minute: dict[str, list[dict]], warmup: int) -> dict[str, list[float]]:
    """The raw per-group ``compute_latest`` ms samples used by the rankings. For each (group, minute) we
    take the SLOWEST shard's time (the critical shard, consistent with the end-to-end view), over the
    post-warmup minutes. Returns ``{group: [ms, ...]}`` — the distribution from which p50/p95/p99 are taken
    (the latency-expectations updater reads this for the full percentile set; ``rank_groups`` reduces it)."""
    minutes_sorted = sorted(by_minute)[warmup:]
    per_group: dict[str, list[float]] = defaultdict(list)
    for minute in minutes_sorted:
        slowest: dict[str, float] = {}
        for rec in by_minute[minute]:
            for name, value in rec.get("group_timings", {}).items():
                if name not in slowest or value > slowest[name]:
                    slowest[name] = value
        for name, value in slowest.items():
            per_group[name].append(value)
    return dict(per_group)


def rank_groups(by_minute: dict[str, list[dict]], warmup: int) -> list[tuple[str, float, float, float]]:
    """Rank non-reduction groups by p50 of their per-group ``compute_latest`` ms (slowest shard per minute,
    over the post-warmup minutes). Returns (name, p50, p99, max) sorted slowest-first."""
    ranked: list[tuple[str, float, float, float]] = []
    for name, values in group_samples(by_minute, warmup).items():
        ranked.append((name, statistics.median(values), _percentile(values, 99), max(values)))
    ranked.sort(key=lambda row: -row[1])
    return ranked


def _report(root: str, n_symbols: int, n_shards: int, warmup: int) -> None:
    bench = Path(root) / "_bench"
    by_minute = _read_shard_records(bench)
    dispatch_walls = _read_dispatch_walls(bench)

    end_to_end = end_to_end_latencies_ms(by_minute, dispatch_walls, warmup)
    print(
        f"\n=== PRE-FLIGHT PROFILE: {n_symbols} symbols, {n_shards} shards "
        f"(~{n_symbols // n_shards}/shard), {len(by_minute)} minutes ===\n"
    )
    print(
        "END-TO-END bar-arrival(last bar) -> universe-vector-ready (slowest shard each minute, "
        "write excluded):"
    )
    if end_to_end:
        p50 = statistics.median(end_to_end)
        p95 = _percentile(end_to_end, 95)
        p99 = _percentile(end_to_end, 99)
        verdict = "PASS" if p99 < BUDGET_MS else "FAIL"
        print(f"    p50={p50:8.1f}ms  p95={p95:8.1f}ms  p99={p99:8.1f}ms  max={max(end_to_end):8.1f}ms")
        print(
            f"    => p99 vs {BUDGET_MS:.0f}ms budget: {verdict} "
            f"({'under' if p99 < BUDGET_MS else f'{p99 / BUDGET_MS:.1f}x over'})"
        )
    else:
        print("    (no end-to-end stamps — was the sim run via this tool with the profiler flags?)")

    print("\nPER-GROUP compute_latest ranking (slowest shard each minute, post-warmup):")
    ranked = rank_groups(by_minute, warmup)
    if not ranked:
        print("    (no per-group timings — FP_SIM_GROUP_TIMINGS was not set)")
    else:
        print(f"    {'group':<28}{'p50':>10}{'p99':>10}{'max':>10}")
        for name, p50, p99, mx in ranked:
            print(f"    {name:<28}{p50:9.2f}ms{p99:9.2f}ms{mx:9.2f}ms")
        print(f"    {'SUM of per-group p50':<28}{sum(row[1] for row in ranked):9.2f}ms")
        top = ", ".join(f"{name} ({p50:.0f}ms)" for name, p50, _, _ in ranked[:3])
        print(f"\n    TOP-3 slowest groups (p50): {top}")


def _run_sim_body(n_symbols: int, n_shards: int, total_minutes: int, window: int, root: str) -> None:
    """The actual sim: bring up the mock, then drive the real streaming path to ``total_minutes`` minutes.
    Runs in the watchdog CHILD (its OWN session/process group) so a hang here can be torn down whole — it
    writes the per-minute bench JSONL to ``root/_bench`` on disk, which the parent re-reads after it returns
    (no result is marshalled back across the process boundary)."""
    symbols = synth_symbols(n_symbols)
    snapshots = {"reference": synth_reference(symbols), "daily": synth_daily(symbols, SESSION_DAY)}

    os.environ["FP_BENCH_LOG"] = "1"
    os.environ["FP_SIM_GROUP_TIMINGS"] = "1"  # the per-group attribution this tool exists to give
    os.environ["STREAM_URL_OVERRIDE"] = f"ws://127.0.0.1:{PORT}"
    os.environ.setdefault("ALPACA_KEY_ID", "mock")
    os.environ.setdefault("ALPACA_SECRET_KEY", "mock")
    # Default to a realistic liquid-name tick firehose (the mock's own default), so the tick path
    # (trade_flow / quote_spread / liquidity / tick_runlength) is stressed at a Monday-like rate, not a
    # token 5/min. Override via MOCK_TRADES_PER_MIN / MOCK_QUOTES_PER_MIN.
    os.environ.setdefault("MOCK_TRADES_PER_MIN", "24")
    os.environ.setdefault("MOCK_QUOTES_PER_MIN", "72")
    os.environ["MOCK_MINUTES"] = str(total_minutes + 2)

    # Own process group: on a watchdog timeout the parent SIGKILLs this whole group, so the daemon mock and
    # the spawned shard workers (our children) cannot be orphaned by a kill of just this process.
    os.setsid()

    _start_mock(total_minutes + 2)
    time.sleep(1.5)
    run_streaming_sim(
        symbols,
        root,
        "mock",
        n_shards=n_shards,
        window=window,
        day=SESSION_DAY,
        max_minutes=total_minutes,
        snapshots=snapshots,
    )


def _sim_timeout_s(measure: int) -> float:
    """The wall-clock cap for this run: an explicit FP_PROFILE_SIM_TIMEOUT_S override, else a scale-aware
    default (a floor plus per-measured-minute headroom) so a HANG is bounded without clipping a legitimately
    long but progressing run."""
    override = os.environ.get("FP_PROFILE_SIM_TIMEOUT_S")
    if override:
        return float(override)
    return max(DEFAULT_TIMEOUT_S, _TIMEOUT_PER_MINUTE_S * measure)


def _group_member_pids(pgid: int) -> list[int]:
    """Every live PID in session group ``pgid`` (the watchdog child + its mock/shard workers). Scans
    ``/proc/*/stat`` field pgrp — used to VERIFY the whole group is gone, then to SIGKILL stragglers by PID
    (killing the group leader does NOT auto-kill the workers, so we reap each member explicitly)."""
    pids: list[int] = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            # comm can contain spaces/parens — split on the LAST ")" so the numeric fields are unambiguous.
            fields = stat_path.read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError, IndexError):
            continue
        # after the "...)" the fields are state(0) ppid(1) pgrp(2) ...; pgrp is index 2 here.
        if len(fields) > 2 and fields[2] == str(pgid):
            try:
                pids.append(int(stat_path.parent.name))
            except ValueError:
                continue
    return pids


def _kill_process_group(child: mp.Process) -> None:
    """Hard-kill the watchdog child AND every process in its session group (the mock + shard workers it
    spawned), so a timed-out run can NEVER leak a CPU core. The child is the group leader (it called
    ``os.setsid``).

    Killing the leader does NOT reap the workers it spawned, and once the leader dies the workers re-parent
    to init and (depending on timing) can drop out of the group scan — that escape is exactly how a core
    leaks. So we SNAPSHOT every group member BEFORE killing the leader, then SIGKILL the leader AND every
    snapshotted PID, and verify nothing in the snapshot survives."""
    pgid = child.pid
    if pgid is None:
        return
    # Snapshot the whole group (leader + workers + mock) while the leader is still alive and holding them.
    members = set(_group_member_pids(pgid))
    members.add(pgid)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        child.join(timeout=0.2)  # reap the leader so it is not left a zombie
        # Re-scan in case the workers spawned more (or were not yet in the group at snapshot time).
        survivors = {pid for pid in members | set(_group_member_pids(pgid)) if _pid_alive(pid)}
        if not survivors:
            return
        for pid in survivors:  # SIGKILL each straggler BY PID — re-parented workers escape the group kill
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    child.kill()
    child.join(timeout=3)


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live process — NOT a zombie. A SIGKILLed worker that re-parented to init is a
    zombie (state Z) until reaped; counting it alive would spin the teardown loop, so read /proc state and
    treat Z (and a missing entry) as dead."""
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        state = stat_path.read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != "Z"


def run_profile_sim_raw(
    n_symbols: int, n_shards: int, measure: int, warmup: int, window: int, root: str
) -> tuple[dict[str, list[dict]], dict[str, float]]:
    """Fake-run the REAL streaming path (msgpack mock -> StockDataStream -> shard workers -> incremental
    fast path) and return the RAW per-minute bench records ``(by_minute, dispatch_walls)``. Both the
    reduced entry point ``run_profile_sim`` and the latency-expectations updater (which needs the full
    per-group distribution for p50/p95/p99) build on this, so they all drive the IDENTICAL compute path —
    no mock, no second code path.

    The sim runs in a WATCHDOG child process (its own session group) with a hard wall-clock cap: if the run
    hangs (mock fails to bind / drops the socket before ``max_minutes`` so the reader's ``stream.run()``
    spins on alpaca-py's reconnect loop forever), the cap fires, the whole child group is SIGKILLed, and a
    ``TimeoutError`` is raised — so a profile_sim / latency-harness run can never leak a core for hours."""
    total_minutes = warmup + measure
    timeout_s = _sim_timeout_s(measure)

    # spawn (not fork) matches the shard workers' own context and stays safe even though the caller may be
    # multithreaded (polars); the child re-imports this module and runs _run_sim_body fresh.
    ctx = mp.get_context("spawn")
    child = ctx.Process(
        target=_run_sim_body,
        args=(n_symbols, n_shards, total_minutes, window, root),
        daemon=False,  # NOT daemon: we own its teardown explicitly; daemon children cannot have children
    )
    child.start()
    child.join(timeout=timeout_s)
    if child.is_alive():
        _kill_process_group(child)
        raise TimeoutError(
            f"profile_sim run exceeded its {timeout_s:.0f}s wall-clock cap "
            f"({n_symbols} syms / {n_shards} shards / {total_minutes} minutes) and was killed (with its "
            "mock + shard-worker children). This is a HANG — almost always the mock failing to serve "
            "(port collision / early socket close) so the reader's stream.run() spins on alpaca-py's "
            "reconnect loop. Set FP_PROFILE_SIM_TIMEOUT_S to adjust the cap for a legitimately long run."
        )
    if child.exitcode not in (0, None):
        raise RuntimeError(f"profile_sim child exited with code {child.exitcode}")

    bench = Path(root) / "_bench"
    return _read_shard_records(bench), _read_dispatch_walls(bench)


def run_profile_sim(
    n_symbols: int, n_shards: int, measure: int, warmup: int, window: int, root: str
) -> tuple[list[float], list[tuple[str, float, float, float]]]:
    """Fake-run the REAL streaming path and return ``(end_to_end_latencies_ms, group_ranking)`` for the
    post-warmup minutes. This is the single entry point both ``main`` (CLI report) and the e2e latency
    regression gate (``tests/test_fp_latency_e2e.py``) drive."""
    by_minute, dispatch_walls = run_profile_sim_raw(n_symbols, n_shards, measure, warmup, window, root)
    return (
        end_to_end_latencies_ms(by_minute, dispatch_walls, warmup),
        rank_groups(by_minute, warmup),
    )


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: python -m quantlib.features.profile_sim <n_symbols> <n_shards> <measure_minutes> "
            "[warmup] [window]"
        )
    n_symbols, n_shards, measure = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    warmup = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    window = int(sys.argv[5]) if len(sys.argv) > 5 else DEFAULT_BUFFER_MINUTES
    root = os.environ.get("BENCH_ROOT", "/tmp/profile_sim_store")

    print(
        f"pre-flight profiling {n_symbols} symbols x {warmup + measure} minutes "
        f"(warmup {warmup}, window {window}); root={root}",
        flush=True,
    )
    run_profile_sim(n_symbols, n_shards, measure, warmup, window, root)
    _report(root, n_symbols, n_shards, warmup)


if __name__ == "__main__":
    main()
