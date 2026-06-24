"""Tests for the feature-worker priority QUEUE ordering (quantlib/features/feature_queue.py).

The ordering is a pure function over the four lifecycle states (no DB), so the priority logic — DIVERGENT
first, fully-trusted excluded, live-locked excluded, stale-lock re-offered — is pinned offline. The DB shell
(queue_snapshot/next_group) is verified only for its dry-run inertness (it must not touch the database).
"""

from __future__ import annotations

from quantlib.features import feature_queue
from quantlib.features.feature_queue import QueuePriority, order_groups


def _counts() -> dict[str, int]:
    """Five groups, two features each — the universe the ordering ranks."""
    return {"alpha": 2, "bravo": 2, "charlie": 2, "delta": 2, "echo": 2}


def test_priority_order_divergent_first_trusted_excluded() -> None:
    # alpha: DIVERGENT (open defect). bravo: UNVERIFIED. charlie: certified-pending-trust.
    # delta: fully trusted -> excluded. echo: stale lock -> MONITORING_STALE.
    items = order_groups(
        feature_counts=_counts(),
        trusted_features_by_group={"charlie": 1, "delta": 2},
        open_defect_groups={"alpha": 3},
        cert_status_by_group={"charlie": ("certified", "2026-06-23")},
        active_locks={"echo": ("dead-agent", True)},
    )
    by_group = {item.group_name: item for item in items}
    assert "delta" not in by_group  # fully trusted -> off the queue
    assert by_group["alpha"].priority == QueuePriority.DIVERGENT
    assert by_group["bravo"].priority == QueuePriority.UNVERIFIED
    assert by_group["echo"].priority == QueuePriority.MONITORING_STALE
    assert by_group["charlie"].priority == QueuePriority.CERTIFIED_PENDING_TRUST
    # the queue is sorted by priority then name: alpha (0) is the head a free worker picks first.
    assert [item.group_name for item in items] == ["alpha", "bravo", "echo", "charlie"]
    assert by_group["alpha"].n_open_defects == 3


def test_live_lock_excludes_group_from_queue() -> None:
    # A LIVE (non-stale) lock owned by another worker excludes the group: two workers never contend.
    items = order_groups(
        feature_counts={"alpha": 2, "bravo": 2},
        trusted_features_by_group={},
        open_defect_groups={},
        cert_status_by_group={},
        active_locks={"alpha": ("busy-worker", False)},  # alpha is live-locked
    )
    groups = {item.group_name for item in items}
    assert groups == {"bravo"}  # alpha excluded — it is being worked right now


def test_divergent_outranks_even_a_live_lock_only_when_stale() -> None:
    # A DIVERGENT group that is ALSO live-locked stays excluded (someone owns it); only when the lock is
    # stale does it re-enter — and then as DIVERGENT (the open defect dominates the phase, not the stale lock).
    live = order_groups(
        feature_counts={"alpha": 2},
        trusted_features_by_group={},
        open_defect_groups={"alpha": 1},
        cert_status_by_group={},
        active_locks={"alpha": ("owner", False)},
    )
    assert live == []  # live-locked -> excluded despite the defect

    stale = order_groups(
        feature_counts={"alpha": 2},
        trusted_features_by_group={},
        open_defect_groups={"alpha": 1},
        cert_status_by_group={},
        active_locks={"alpha": ("dead", True)},
    )
    assert len(stale) == 1
    assert stale[0].priority == QueuePriority.DIVERGENT  # defect dominates the stale lock


def test_certified_but_not_all_trusted_is_pending_trust() -> None:
    # certified within-day, but only 1/2 features trusted -> still on the queue, lowest priority.
    items = order_groups(
        feature_counts={"alpha": 2},
        trusted_features_by_group={"alpha": 1},
        open_defect_groups={},
        cert_status_by_group={"alpha": ("certified", "2026-06-23")},
        active_locks={},
    )
    assert len(items) == 1
    assert items[0].priority == QueuePriority.CERTIFIED_PENDING_TRUST
    assert items[0].cert_status == "certified"


def test_empty_universe_is_empty_queue() -> None:
    assert order_groups({}, {}, {}, {}, {}) == []


def test_queue_snapshot_dry_run_touches_no_db() -> None:
    # dry-run must short-circuit before any psycopg.connect — an empty queue, no DB.
    assert feature_queue.queue_snapshot(dry_run=True) == []
    assert feature_queue.next_group(dry_run=True) is None
