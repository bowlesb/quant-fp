"""FAIL-SAFE hardening for the zero-gap deploy seam — a deploy attempt must NEVER crash live capture.

The whole point of the seam is ZERO capture gap. A corollary the happy-path tests do not cover: the deploy
machinery itself must be fail-safe — ANY exception raised while applying a job (a diverged live tree whose
``--ff-only`` merge raises ``CalledProcessError``, a transient DB error in ``gather_live_evidence`` /
``claim_next``, an unexpected error in a callback) must be CONTAINED to that one job (escalated + logged) and
the capture loop must keep running. If an exception propagates out of ``poll_and_apply_at_boundary`` it
reaches ``on_bar`` / ``process_bars``'s caller and can break the capture stream — the exact gap the seam
exists to prevent.

Two hazards this suite pins:

  1. ``apply_job`` calls ``do_merge`` OUTSIDE the try/except that guards ``do_swap``. A merge failure (the
     documented diverged-tree case — ``live_do_merge``'s ``--ff-only`` raises rather than clobbering a pinned
     tree) must ESCALATE the job, not propagate. NO swap may happen on a failed merge.
  2. ``poll_and_apply_at_boundary`` must defend in depth: even an exception from a place ``apply_job`` does
     not guard (``claim_next``, ``gather_live_evidence``, ``record_outcome``) must not escape the seam — the
     boundary returns a logged outcome and capture continues.
"""

from __future__ import annotations

import subprocess

import pytest

import quantlib.features.groups  # noqa: F401  populate REGISTRY
from quantlib.features import within_day_live_wiring as wiring
from quantlib.features.capture import CaptureState
from quantlib.features.registry import REGISTRY
from quantlib.features.within_day_applier import DeployJob, apply_job
from quantlib.features.within_day_deploy_queue import QueuedJob
from quantlib.features.within_day_scope_guard import GateEvidence

GROUP = "momentum"


def _clean_evidence() -> GateEvidence:
    fp = 0x873F2FCEB8F00C92
    return GateEvidence(
        group_name=GROUP,
        owned_feature="up_ratio_3m",
        changed_files=[f"quantlib/features/groups/{GROUP}.py"],
        owned_file_set=[f"quantlib/features/groups/{GROUP}.py"],
        fingerprint_before=fp,
        fingerprint_after=fp,
        parity_was_mismatch=True,
        parity_now_clean=True,
        differing_other_groups=[],
        owned_feature_is_untrusted=True,
        trusted_features_moved=[],
        unit_tests_passed=True,
        qa_clean=True,
        hot_swap_safe=True,
    )


# ---- hazard 1: a failed merge ESCALATES (no swap), it does NOT propagate -------------------------


def test_apply_job_merge_failure_escalates_no_swap() -> None:
    """A merge that raises (the diverged-tree ``--ff-only`` case) must escalate the job with NO swap — not
    propagate the CalledProcessError into the caller (which is the capture loop)."""
    job = DeployJob(job_id=1, group_name=GROUP, agent_id="a", commit_sha="cafe")
    swapped: list[str] = []

    def _failing_merge(_dep_job: DeployJob) -> None:
        raise subprocess.CalledProcessError(1, ["git", "merge", "--ff-only"], stderr="diverged")

    outcome = apply_job(
        job,
        evidence=_clean_evidence(),
        do_swap=lambda group: swapped.append(group) or (_ for _ in ()).throw(AssertionError("swapped!")),
        do_merge=_failing_merge,
        confirm_tripwire=lambda group: True,
        rollback_swap=lambda group: None,
    )
    assert outcome.status == "escalated"
    assert "merge" in outcome.detail.lower()
    assert swapped == []  # the swap must NOT run when the merge failed


# ---- hazard 2: the SEAM contains ANY per-job exception; capture continues ------------------------


def test_seam_contains_merge_failure_does_not_crash_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: an armed seam whose job has a diverged tree (merge raises) returns a logged outcome and
    does NOT propagate — the registry is untouched (no swap on a failed merge) and the seam survives."""
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    jobs = [QueuedJob(job_id=2, group_name=GROUP, agent_id="a", commit_sha="dead", fail_count=0)]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    monkeypatch.setattr(
        wiring.within_day_deploy_run, "record_outcome", lambda outcome, job, dry_run=True: None
    )
    # Force the merge to raise as a diverged --ff-only would, even in dry_run.
    monkeypatch.setattr(
        wiring,
        "live_do_merge",
        lambda dep_job, config: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["git", "merge", "--ff-only"])
        ),
    )
    swapped: list[str] = []
    monkeypatch.setattr(wiring, "live_do_swap", lambda state, group, config: swapped.append(group))

    state = CaptureState()
    before = REGISTRY.get_group(GROUP)
    # Must NOT raise — the seam contains the failure.
    outcomes = wiring.poll_and_apply_at_boundary(
        state,
        wiring.LiveSwapConfig(
            feature_root="/tmp/zg",
            feature_tree="/tmp/zg-tree",
            sample_symbols=["AAA"],
            dry_run=True,
        ),
    )
    after = REGISTRY.get_group(GROUP)
    assert len(outcomes) == 1 and "escalated" in outcomes[0]
    assert swapped == []  # no swap on a failed merge
    assert after is before  # registry untouched


def test_seam_contains_unexpected_evidence_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected error from a place ``apply_job`` does not guard (here ``gather_live_evidence``, standing
    in for a transient DB error) must still not escape the seam — the boundary logs and capture continues."""
    monkeypatch.setenv(wiring.LIVE_SWAP_ENV, "1")
    jobs = [QueuedJob(job_id=3, group_name=GROUP, agent_id="a", commit_sha="beef", fail_count=0)]
    monkeypatch.setattr(wiring, "claim_next", lambda **_k: jobs.pop(0) if jobs else None)
    monkeypatch.setattr(
        wiring.within_day_deploy_run, "record_outcome", lambda outcome, job, dry_run=True: None
    )
    monkeypatch.setattr(
        wiring,
        "gather_live_evidence",
        lambda job, config: (_ for _ in ()).throw(RuntimeError("transient DB error")),
    )

    state = CaptureState()
    # Must NOT raise.
    outcomes = wiring.poll_and_apply_at_boundary(
        state,
        wiring.LiveSwapConfig(
            feature_root="/tmp/zg",
            feature_tree="/tmp/zg-tree",
            sample_symbols=["AAA"],
            dry_run=True,
        ),
    )
    assert len(outcomes) == 1 and "error" in outcomes[0].lower()
