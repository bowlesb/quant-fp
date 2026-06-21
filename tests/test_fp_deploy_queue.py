"""Unit tests for the auto-deploy queue (ops.deploy_queue) — serialized, file-backed, batching.

Properties: enqueue is idempotent on (service, sha); claim_batch coalesces per service to the newest SHA;
TIER-2/coordinated entries are KEPT on the queue (they wait for the relaunch) while TIER-1 ripe entries are
drained; the batch window holds back still-arriving merges. Named test_fp_* so the gate runs these on itself.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def queue(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A fresh queue rooted in a temp dir with a zero batch window (everything ripe immediately)."""
    monkeypatch.setenv("CI_DEPLOY_QUEUE_DIR", str(tmp_path / "dq"))
    monkeypatch.setenv("CI_DEPLOY_BATCH_WINDOW_S", "0")
    import ops.deploy_queue as q

    importlib.reload(q)  # re-read the env-derived module constants
    return q


def test_enqueue_and_peek(queue) -> None:  # type: ignore[no-untyped-def]
    queue.enqueue([queue.DeployEntry.new("dashboard", "tier-1-auto", "aaa", 2)])
    pending = queue.peek()
    assert len(pending) == 1
    assert pending[0].service == "dashboard" and pending[0].sha == "aaa"


def test_enqueue_idempotent_on_service_sha(queue) -> None:  # type: ignore[no-untyped-def]
    entry = queue.DeployEntry.new("dashboard", "tier-1-auto", "aaa", 2)
    queue.enqueue([entry])
    queue.enqueue([entry])  # same (service, sha) → no duplicate
    assert len(queue.peek()) == 1


def test_claim_coalesces_per_service_to_newest(queue) -> None:  # type: ignore[no-untyped-def]
    queue.enqueue([queue.DeployEntry.new("dashboard", "tier-1-auto", "aaa", 2)])
    queue.enqueue([queue.DeployEntry.new("dashboard", "tier-1-auto", "bbb", 3)])
    auto, coord = queue.claim_batch()
    assert [(e.service, e.sha) for e in auto] == [("dashboard", "bbb")]  # newest SHA wins
    assert coord == []


def test_coordinated_kept_for_relaunch_not_drained(queue) -> None:  # type: ignore[no-untyped-def]
    queue.enqueue([queue.DeployEntry.new("dashboard", "tier-1-auto", "aaa", 2)])
    queue.enqueue([queue.DeployEntry.new("feature-computer", "tier-2-coordinated", "ccc", 5)])
    auto, coord = queue.claim_batch()
    assert [e.service for e in auto] == ["dashboard"]
    assert [e.service for e in coord] == ["feature-computer"]
    # the coordinated entry is STILL pending (awaiting the relaunch); the auto one is drained
    remaining = queue.peek()
    assert [e.service for e in remaining] == ["feature-computer"]


def test_drain_coordinated_clears_them(queue) -> None:  # type: ignore[no-untyped-def]
    queue.enqueue([queue.DeployEntry.new("feature-computer", "tier-2-coordinated", "ccc", 5)])
    drained = queue.drain_coordinated()
    assert [e.service for e in drained] == ["feature-computer"]
    assert queue.peek() == []


def test_batch_window_holds_unripe_auto(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # With a long window, a freshly-enqueued auto entry is NOT yet ripe → not claimed (lets a burst coalesce).
    monkeypatch.setenv("CI_DEPLOY_QUEUE_DIR", str(tmp_path / "dq"))
    monkeypatch.setenv("CI_DEPLOY_BATCH_WINDOW_S", "9999")
    import ops.deploy_queue as q

    importlib.reload(q)
    q.enqueue([q.DeployEntry.new("dashboard", "tier-1-auto", "aaa", 2)])
    auto, _ = q.claim_batch()
    assert auto == []  # still within the window → held
    assert len(q.peek()) == 1  # still pending
    importlib.reload(q)  # restore default-env module for other tests


def test_queue_survives_reload_file_backed(queue) -> None:  # type: ignore[no-untyped-def]
    queue.enqueue([queue.DeployEntry.new("news-capture", "tier-1-auto", "ddd", 1)])
    import ops.deploy_queue as q2

    importlib.reload(q2)  # simulate a daemon restart re-reading the file
    # NOTE: reload re-reads env (same CI_DEPLOY_QUEUE_DIR) so it sees the persisted file
    assert any(e.service == "news-capture" for e in q2.peek())
    assert os.path.isfile(q2.QUEUE_FILE)
