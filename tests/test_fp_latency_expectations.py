"""The latency-expectations JSON updater's pure logic — the percentile math, the crypto cross-check
parsing, and the document assembly (schema/ordering/determinism). These are PURE (no sim, no docker), so
they are unit-tested directly; the full per-group + e2e measurement is exercised by ops/remeasure_latency.sh
(the heavy multiprocess sim), not in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from quantlib.features import latency_expectations as le
from quantlib.features.profile_sim import group_samples


def test_pct_nearest_rank() -> None:
    """Nearest-rank on idx round(pct/100 * (n-1)) — the same convention as the sim's _percentile."""
    ordered = [10.0, 20.0, 30.0, 40.0, 50.0]  # n=5
    assert le._pct(ordered, 0) == 10.0  # idx 0
    assert le._pct(ordered, 50) == 30.0  # idx round(0.5*4)=2
    assert le._pct(ordered, 100) == 50.0  # idx 4
    assert le._pct([7.0], 99) == 7.0  # single sample
    assert le._pct([], 50) == 0.0  # empty -> 0, not a crash


def test_group_samples_takes_slowest_shard_per_minute() -> None:
    """The updater's per-group distribution comes from group_samples: slowest shard each minute, warmup
    dropped. Locks the shared helper rank_groups + the updater both build on."""
    by_minute = {
        "m0": [{"group_timings": {"g": 999.0}}],  # dropped by warmup
        "m1": [{"group_timings": {"g": 10.0}}, {"group_timings": {"g": 12.0}}],  # slowest 12
        "m2": [{"group_timings": {"g": 20.0}}, {"group_timings": {"g": 15.0}}],  # slowest 20
    }
    assert group_samples(by_minute, warmup=1) == {"g": [12.0, 20.0]}


def test_harvest_crypto_from_file(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When CRYPTO_COMPUTE_MS_FILE is set (the in-container path) the harvest scrapes compute_ms from the
    pre-harvested host log file and reports p50/p95/p99, not docker."""
    log = tmp_path / "crypto.log"
    log.write_text(
        "minute=2026-06-21T03:37:00 symbols=5 trades=0 compute_ms=400 groups=39\n"
        "minute=2026-06-21T03:38:00 symbols=4 trades=0 compute_ms=800 groups=39\n"
        "some unrelated line without the metric\n"
        "minute=2026-06-21T03:39:00 symbols=3 trades=1 compute_ms=600 groups=39\n"
    )
    monkeypatch.setenv("CRYPTO_COMPUTE_MS_FILE", str(log))
    crosscheck = le.harvest_crypto_crosscheck()
    assert crosscheck["status"] == "ok"
    assert crosscheck["samples"] == 3
    assert crosscheck["p50_ms"] == 600.0


def test_harvest_crypto_unavailable_when_empty(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    log = tmp_path / "empty.log"
    log.write_text("no compute_ms lines here\n")
    monkeypatch.setenv("CRYPTO_COMPUTE_MS_FILE", str(log))
    assert le.harvest_crypto_crosscheck()["status"] == "unavailable"


def test_build_document_schema_and_ordering() -> None:
    """The document is the schema contract DashLatencyView binds to: header keys present, groups sorted by
    p99 desc, every row carries p50/p95/p99, not_measured_groups reconciles the gather group."""
    groups = [
        {
            "group": "slow",
            "feat_count": 3,
            "kind": "B incremental-sum",
            "mechanism": "x",
            "incremental_ready": "ready",
            "p50_ms": 80.0,
            "p95_ms": 95.0,
            "p99_ms": 100.0,
        },
        {
            "group": "fast",
            "feat_count": 2,
            "kind": "A cached/static",
            "mechanism": "y",
            "incremental_ready": "n-a",
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
        },
    ]
    doc = le.build_document(groups, [120.0, 130.0, 140.0], {"status": "skipped"}, "2026-06-21T00:00:00Z")
    for key in (
        "schema_version",
        "generated_at",
        "units",
        "sorted_by",
        "measurement",
        "e2e_context",
        "live_crypto_crosscheck",
        "group_count",
        "feature_count",
        "not_measured_groups",
        "groups",
    ):
        assert key in doc, f"missing header key {key}"
    assert doc["schema_version"] == le.SCHEMA_VERSION
    p99s = [row["p99_ms"] for row in doc["groups"]]
    assert p99s == sorted(p99s, reverse=True), "groups must be sorted slowest-first by p99"
    assert all(all(k in row for k in ("p50_ms", "p95_ms", "p99_ms")) for row in doc["groups"])
    assert doc["e2e_context"]["measured_at_sim_scale"]["p50_ms"] == 130.0
    assert doc["group_count"] == 2 and doc["feature_count"] == 5


def test_build_document_deterministic() -> None:
    """Same inputs -> byte-identical JSON (the loop rewrites deterministically)."""
    groups = [
        {
            "group": "g",
            "feat_count": 1,
            "kind": "k",
            "mechanism": "m",
            "incremental_ready": "n-a",
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
        }
    ]
    a = json.dumps(le.build_document(groups, [5.0], {"status": "skipped"}, "2026-06-21T00:00:00Z"), indent=2)
    b = json.dumps(le.build_document(groups, [5.0], {"status": "skipped"}, "2026-06-21T00:00:00Z"), indent=2)
    assert a == b
