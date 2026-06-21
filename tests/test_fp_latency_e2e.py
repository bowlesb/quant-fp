"""End-to-end bar->vector latency REGRESSION GATE (docs/latency_e2e_budget.yaml).

The companion to test_fp_latency_budget.py: that gate catches a single GROUP regressing in isolation
(per-group compute_latest budgets); THIS gate catches the END-TO-END number a bet actually pays — the
wall-clock from a minute's last bar arriving to the universe-wide feature vector being READY (slowest
shard each minute, parquet write excluded). It drives the REAL streaming path — the protocol-faithful
msgpack mock -> a real StockDataStream -> the same shard workers -> the incremental fast path, via the
SAME quantlib.features.profile_sim.run_profile_sim the pre-open CLI uses — so it is a faithful e2e
measurement, NOT a mock of compute.

It is a CEILING gate, not the 100ms target: it asserts the measured p50/p99 stay under a generous
current-reality ceiling (docs/latency_e2e_budget.yaml) so it PASSES at today's latency and only TRIPS on
a real >~1.6x e2e regression. The <100ms aspirational target is stated in the budget file and the report
banner but is NOT enforced here (it would fail today).

HEAVY / OPT-IN: spins up a multiprocess sim (mock server + N shard workers), so it is gated behind
``FP_LATENCY_E2E=1`` and skipped by default — it is NOT part of the fast unit suite. Run it via
``make fp-latency-e2e`` (or set the env var and select this file). The bounded reference scale
(256 syms / 8 shards / 10 measured minutes) keeps it to ~10-15s of wall in the sandbox.

When it breaches: it is a REGRESSION to investigate (a slow group, a lost incremental emit, a gather/IPC
blowup) — re-run the full ``make fp-profile-sim`` pre-flight and the per-group ``make fp-profile-latest``
to attribute the stage, then fix worktree->PR. Re-seed the ceiling only on a DELIBERATE, reviewed change.
"""
from __future__ import annotations

import os
import statistics
from pathlib import Path

import pytest

from quantlib.features.profile_sim import run_profile_sim
from quantlib.features.stream_sim import _percentile

_BUDGET_PATH = Path(__file__).resolve().parents[1] / "docs" / "latency_e2e_budget.yaml"

pytestmark = pytest.mark.skipif(
    os.environ.get("FP_LATENCY_E2E") != "1",
    reason="heavy multiprocess e2e latency sim — opt in with FP_LATENCY_E2E=1 (make fp-latency-e2e)",
)


def _parse_e2e_budget(path: Path) -> tuple[dict[str, int], dict[str, float]]:
    """Minimal parser for the flat e2e budget YAML (same no-dependency style as the per-group budget
    parser): the ``reference_scale:`` block of int scalars and the ``ceilings_ms:`` block of ``key: float``.
    Other top-level blocks (aspirational_target_ms / stage_notes / comments) are ignored. Raises on a
    malformed line so a typo in the budget fails LOUDLY, not silently."""
    scale: dict[str, int] = {}
    ceilings: dict[str, float] = {}
    section: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            section = line.rstrip(":").strip()
            continue
        key, _, value = line.strip().partition(":")
        key, value = key.strip(), value.strip()
        if section == "reference_scale":
            scale[key] = int(value)
        elif section == "ceilings_ms":
            ceilings[key] = float(value)
    if not scale or not ceilings:
        raise ValueError(f"missing reference_scale or ceilings_ms in {path}")
    return scale, ceilings


def test_e2e_bar_to_vector_under_ceiling(tmp_path: Path) -> None:
    scale, ceilings = _parse_e2e_budget(_BUDGET_PATH)
    end_to_end, ranking = run_profile_sim(
        n_symbols=scale["n_symbols"],
        n_shards=scale["n_shards"],
        measure=scale["measure_minutes"],
        warmup=scale["warmup_minutes"],
        window=scale["window_min"],
        root=str(tmp_path / "e2e_store"),
    )
    assert end_to_end, (
        "no end-to-end latency stamps were produced — the sim did not run the instrumented path "
        "(check the mock server started and FP_BENCH_LOG/FP_SIM_GROUP_TIMINGS were set by run_profile_sim)"
    )

    p50 = statistics.median(end_to_end)
    p99 = _percentile(end_to_end, 99)
    top = ", ".join(f"{name} ({group_p50:.0f}ms p50)" for name, group_p50, _, _ in ranking[:3])
    detail = (
        f"e2e bar->vector over {len(end_to_end)} post-warmup minutes at "
        f"{scale['n_symbols']} syms / {scale['n_shards']} shards: p50={p50:.0f}ms p99={p99:.0f}ms "
        f"(ceilings p50<{ceilings['p50']:.0f} p99<{ceilings['p99']:.0f}; aspirational target p99<100). "
        f"top groups: {top}"
    )
    print("\n" + detail)

    breaches = []
    if p50 > ceilings["p50"]:
        breaches.append(f"p50 {p50:.0f}ms > ceiling {ceilings['p50']:.0f}ms")
    if p99 > ceilings["p99"]:
        breaches.append(f"p99 {p99:.0f}ms > ceiling {ceilings['p99']:.0f}ms")
    assert not breaches, (
        "END-TO-END bar->vector latency REGRESSED past its docs/latency_e2e_budget.yaml ceiling "
        f"({'; '.join(breaches)}). {detail}. Re-run `make fp-profile-sim` + `make fp-profile-latest` "
        "to attribute the slow stage, then fix worktree->PR. Re-seed the ceiling ONLY on a deliberate, "
        "reviewed change."
    )
