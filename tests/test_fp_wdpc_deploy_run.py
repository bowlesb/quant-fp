"""Unit tests for the WDPC queue-wired applier orchestration (quantlib.features.within_day_deploy_run).

Verifies the connective tissue WITHOUT any live state: process_one maps an ApplyOutcome to the right queue
terminal mark and fires the version-reset only on 'applied'; run_queue is the serialized FIFO loop draining a
fake queue one job at a time; the default actions are fully inert (no swap, confirm True); the live hot-swap
is a documented Lead-gated seam (live_action_seam), never invoked here. Named test_fp_* for the CI gate.
"""

from __future__ import annotations

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.features import within_day_deploy_run as run
from quantlib.features import within_day_deploy_queue as queue
from quantlib.features import within_day_version
from quantlib.features.within_day_applier import ApplyOutcome
from quantlib.features.within_day_deploy_queue import QueuedJob
from quantlib.features.within_day_scope_guard import GateEvidence


def _approving_evidence(group_name: str, feature: str, owned_file: str) -> GateEvidence:
    """A GateEvidence that passes every §4 condition (fingerprint unchanged, parity flipped, byte-eq
    elsewhere, untrusted feature, tests+QA green, hot-swap-safe) — the pipeline therefore reaches 'applied'."""
    return GateEvidence(
        group_name=group_name,
        owned_feature=feature,
        changed_files=[owned_file],
        owned_file_set=[owned_file],
        fingerprint_before=0x1234,
        fingerprint_after=0x1234,
        parity_was_mismatch=True,
        parity_now_clean=True,
        differing_other_groups=[],
        owned_feature_is_untrusted=True,
        trusted_features_moved=[],
        unit_tests_passed=True,
        qa_clean=True,
        hot_swap_safe=True,
    )


def _failing_evidence() -> GateEvidence:
    """Evidence that fails the scope-guard (a non-owned file in the diff) → the pipeline escalates."""
    ev = _approving_evidence("momentum", "mom_5m", "quantlib/features/groups/momentum.py")
    ev.changed_files = ["quantlib/features/incremental.py"]  # shared code → out of owned scope
    return ev


_JOB = QueuedJob(job_id=42, group_name="momentum", agent_id="agent-7", commit_sha="abc123", fail_count=0)


def test_process_one_applied_marks_applied_and_resets_trust(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    marks: list[tuple[str, int]] = []
    monkeypatch.setattr(queue, "mark_applied", lambda job_id, detail="", dry_run=True: marks.append(("applied", job_id)))
    monkeypatch.setattr(queue, "mark_rolled_back", lambda *a, **k: marks.append(("rolled_back", 0)))
    monkeypatch.setattr(queue, "mark_escalated", lambda *a, **k: marks.append(("escalated", 0)))
    reset_calls: list[str] = []
    monkeypatch.setattr(
        within_day_version, "reset_trust_on_content_change", lambda group, dry_run=True: reset_calls.append(group.name) or []
    )

    evidence = _approving_evidence("momentum", "mom_5m", "quantlib/features/groups/momentum.py")
    outcome = run.process_one(_JOB, evidence=evidence, actions=run.dry_run_actions())

    assert outcome.status == "applied"
    assert marks == [("applied", 42)]
    assert reset_calls == ["momentum"]  # the version-reset fired (content hash changed → re-earn)


def test_process_one_escalated_marks_escalated_and_no_reset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    marks: list[str] = []
    monkeypatch.setattr(queue, "mark_applied", lambda *a, **k: marks.append("applied"))
    monkeypatch.setattr(queue, "mark_escalated", lambda job_id, detail="", dry_run=True: marks.append("escalated"))
    reset_calls: list[str] = []
    monkeypatch.setattr(
        within_day_version, "reset_trust_on_content_change", lambda group, dry_run=True: reset_calls.append(group.name) or []
    )

    outcome = run.process_one(_JOB, evidence=_failing_evidence(), actions=run.dry_run_actions())

    assert outcome.status == "escalated"
    assert marks == ["escalated"]
    assert reset_calls == []  # nothing deployed → no version-reset


def test_process_one_rolled_back_marks_rolled_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    marks: list[str] = []
    monkeypatch.setattr(queue, "mark_rolled_back", lambda job_id, detail="", dry_run=True: marks.append("rolled_back"))
    monkeypatch.setattr(queue, "mark_applied", lambda *a, **k: marks.append("applied"))

    # The tripwire fails post-swap → apply_job rolls back → process_one marks rolled_back.
    actions = run.dry_run_actions()
    actions["confirm_tripwire"] = lambda group_name: False

    outcome = run.process_one(
        _JOB, evidence=_approving_evidence("momentum", "mom_5m", "quantlib/features/groups/momentum.py"), actions=actions
    )
    assert outcome.status == "rolled_back"
    assert marks == ["rolled_back"]


def test_run_queue_drains_fifo_one_at_a_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    jobs = [
        QueuedJob(job_id=1, group_name="momentum", agent_id="a", commit_sha="s1", fail_count=0),
        QueuedJob(job_id=2, group_name="volatility", agent_id="b", commit_sha="s2", fail_count=0),
    ]
    claimed: list[QueuedJob] = []

    def fake_claim(*, dry_run=True):  # type: ignore[no-untyped-def]
        return jobs.pop(0) if jobs else None

    monkeypatch.setattr(queue, "claim_next", fake_claim)
    processed: list[ApplyOutcome] = []
    monkeypatch.setattr(
        run, "process_one", lambda job, *, evidence, actions, dry_run=True: processed.append(job) or ApplyOutcome(job.job_id, job.group_name, "applied", "")
    )

    outcomes = run.run_queue(lambda job: claimed.append(job) or None)  # type: ignore[arg-type, func-returns-value]
    assert [o.job_id for o in outcomes] == [1, 2]  # FIFO order preserved, drained to empty


def test_run_queue_dry_run_is_a_noop() -> None:
    # dry_run claim_next returns None → the loop never enters → no actions, no DB.
    outcomes = run.run_queue(lambda job: None)  # type: ignore[arg-type, return-value]
    assert outcomes == []


def test_dry_run_actions_are_inert() -> None:
    actions = run.dry_run_actions()
    result = actions["do_swap"]("momentum")
    assert result.swapped is False and result.reseeded is False
    assert actions["confirm_tripwire"]("momentum") is True


def test_live_action_seam_is_documented_not_wired() -> None:
    seam = run.live_action_seam()
    assert "apply_in_running_loop" in seam and "Lead/Ben-gated" in seam
