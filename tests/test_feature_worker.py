"""Tests for the feature-worker ORCHESTRATOR (quantlib/features/feature_worker.py).

The worker strings the queue + the lifecycle into one loop. We stub the queue (next_group), the lifecycle
(run_group_lifecycle), and the DIVERGENT triage so the worker's branching + loop control are verified with
no DB and no live state:

  * a CLEAN group -> run_group_lifecycle, surfaced as advanced iff it certified;
  * a DIVERGENT group -> a read-only root-cause triage, NEVER the lifecycle (no code auto-edited);
  * a lost claim race -> skipped (claimed False), the loop continues;
  * ``--once`` advances exactly one group; the loop stops on an empty queue / max_iterations.
"""

from __future__ import annotations

from quantlib.features import feature_worker
from quantlib.features.feature_queue import QueueItem, QueuePriority
from quantlib.features.within_day_rootcause import ARTIFACT, LIVE_FAST_PATH, RootCause
from quantlib.features.within_day_run import LifecycleResult
from quantlib.features.within_day_trust import CertResult


def _item(group: str, priority: QueuePriority, n_open: int = 0) -> QueueItem:
    return QueueItem(
        group_name=group,
        priority=priority,
        n_features=2,
        n_trusted=0,
        n_open_defects=n_open,
        cert_status=None,
        cert_day=None,
        owner=None,
        owner_stale=False,
    )


def _cert(group: str) -> CertResult:
    return CertResult(
        feature="f1",
        version="1.0.0",
        group_name=group,
        cert_day="2026-06-23",
        status="certified",
        value_rate=1.0,
        stable_cycles=3,
        window_minutes=20,
        n_clean_symbols=10,
        n_compared=400,
        settle_lag_min=20.0,
    )


def test_clean_group_runs_lifecycle_and_reports_certify(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def fake_lifecycle(feature_root, group, agent, **kw):  # type: ignore[no-untyped-def]
        captured["group"] = group
        captured["agent"] = agent
        return LifecycleResult(
            group_name=group, agent_id=agent, claimed=True, certified=_cert(group), reset_features=["f1"]
        )

    monkeypatch.setattr(feature_worker.within_day_run, "run_group_lifecycle", fake_lifecycle)
    result = feature_worker.advance_group(_item("breadth", QueuePriority.UNVERIFIED), "w1")

    assert result.claimed is True
    assert result.advanced is True  # certified -> advanced a phase
    assert captured["group"] == "breadth"
    assert "CERTIFIED" in result.detail


def test_divergent_group_triages_and_never_runs_lifecycle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ran_lifecycle: list[str] = []
    monkeypatch.setattr(
        feature_worker.within_day_run,
        "run_group_lifecycle",
        lambda *a, **k: ran_lifecycle.append("ran"),
    )
    monkeypatch.setattr(
        feature_worker,
        "triage_divergent_group",
        lambda group, dry_run: [
            RootCause("f1", LIVE_FAST_PATH, ("incremental.py",), "eps", 5, 0, 0, None),
            RootCause("f2", ARTIFACT, (), "warm-up", 1, 0, 0, None),
        ],
    )
    result = feature_worker.advance_group(_item("price_volume", QueuePriority.DIVERGENT, n_open=2), "w1")

    assert ran_lifecycle == []  # a DIVERGENT group NEVER runs the certify lifecycle
    assert result.claimed is False  # triage is read-only, no lock taken
    assert result.advanced is False  # triaged, not certified — the fix is the next step
    assert len(result.triage) == 2
    assert "1/2 actionable" in result.detail  # only the LIVE_FAST_PATH cause is actionable


def test_lost_claim_race_is_skipped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        feature_worker.within_day_run,
        "run_group_lifecycle",
        lambda *a, **k: LifecycleResult(group_name="g", agent_id="w1", claimed=False),
    )
    result = feature_worker.advance_group(_item("breadth", QueuePriority.UNVERIFIED), "w1")
    assert result.claimed is False
    assert result.advanced is False
    assert "claim lost" in result.detail


def test_run_worker_once_advances_exactly_one_group(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    served = [
        _item("alpha", QueuePriority.UNVERIFIED),
        _item("bravo", QueuePriority.UNVERIFIED),
    ]
    calls = iter(served)
    monkeypatch.setattr(feature_worker.feature_queue, "next_group", lambda dry_run: next(calls, None))
    advanced: list[str] = []
    monkeypatch.setattr(
        feature_worker,
        "advance_group",
        lambda item, agent, **kw: advanced.append(item.group_name)
        or feature_worker.AdvanceResult(item.group_name, item.phase, True, True, "ok"),
    )

    results = feature_worker.run_worker("w1", once=True, dry_run_cert=True)
    assert len(results) == 1  # --once advances exactly one group
    assert advanced == ["alpha"]


def test_run_worker_loops_until_queue_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    served = [_item("alpha", QueuePriority.UNVERIFIED), _item("bravo", QueuePriority.DIVERGENT, 1)]
    calls = iter(served)
    monkeypatch.setattr(feature_worker.feature_queue, "next_group", lambda dry_run: next(calls, None))
    monkeypatch.setattr(
        feature_worker,
        "advance_group",
        lambda item, agent, **kw: feature_worker.AdvanceResult(
            item.group_name, item.phase, True, True, "ok"
        ),
    )
    # max_iterations guards against an infinite loop; the queue empties after 2 so it stops on its own.
    results = feature_worker.run_worker("w1", max_iterations=10, dry_run_cert=True)
    assert [r.group_name for r in results] == ["alpha", "bravo"]


def test_make_agent_id_is_unique_and_prefixed() -> None:
    first = feature_worker.make_agent_id()
    second = feature_worker.make_agent_id()
    assert first.startswith("fworker-")
    assert first != second  # the uuid suffix makes each worker distinct on the lock


def test_triage_divergent_dry_run_touches_no_db() -> None:
    assert feature_worker.triage_divergent_group("price_volume", dry_run=True) == []
