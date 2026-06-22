"""Unit tests for the WDPC FIFO deploy queue (quantlib.features.within_day_deploy_queue).

Properties tested PURELY (no DB — dry_run is the default, and the backoff decision is a pure function of
fail_count): every mutation is inert in dry_run (no connection); enqueue/claim_next/pending return the dry-run
sentinels; mark_failed escalates at MAX_FAIL_COUNT and re-enqueues below it; _mark_terminal rejects a
non-terminal status. Named test_fp_* so the CI gate runs these on itself.
"""

from __future__ import annotations

import logging

import pytest

from quantlib.features import within_day_deploy_queue as queue
from quantlib.features.within_day_deploy_queue import MAX_FAIL_COUNT, QueuedJob


def test_enqueue_dry_run_returns_none_and_writes_nothing(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO, logger="within_day_deploy_queue"):
        result = queue.enqueue("momentum", "agent-7", "abc123")
    assert result is None
    assert "DRY-RUN enqueue" in caplog.text


def test_claim_next_dry_run_returns_none() -> None:
    assert queue.claim_next() is None


def test_pending_dry_run_returns_empty() -> None:
    assert queue.pending() == []


def test_mark_terminal_states_are_inert_in_dry_run() -> None:
    assert queue.mark_applied(1) is True
    assert queue.mark_rolled_back(2) is True
    assert queue.mark_escalated(3) is True


def test_mark_failed_escalates_at_the_retry_ceiling() -> None:
    # fail_count is the job's CURRENT count; the (fail_count + 1) reaching MAX_FAIL_COUNT escalates.
    status = queue.mark_failed(job_id=9, fail_count=MAX_FAIL_COUNT - 1)
    assert status == queue.STATUS_ESCALATED


def test_mark_failed_below_ceiling_re_enqueues() -> None:
    status = queue.mark_failed(job_id=9, fail_count=0)
    assert status == queue.STATUS_QUEUED


def test_mark_failed_is_a_pure_function_of_fail_count() -> None:
    # Every count below the ceiling re-enqueues; the ceiling-1 (and above) escalates. No DB needed.
    for count in range(MAX_FAIL_COUNT - 1):
        assert queue.mark_failed(job_id=1, fail_count=count) == queue.STATUS_QUEUED
    assert queue.mark_failed(job_id=1, fail_count=MAX_FAIL_COUNT - 1) == queue.STATUS_ESCALATED


def test_mark_terminal_rejects_a_non_terminal_status() -> None:
    with pytest.raises(ValueError, match="non-terminal"):
        queue._mark_terminal(1, queue.STATUS_QUEUED, "", dry_run=True)


def test_queued_job_is_a_plain_record() -> None:
    job = QueuedJob(job_id=1, group_name="momentum", agent_id="a", commit_sha="sha", fail_count=0)
    assert (job.job_id, job.group_name, job.agent_id, job.commit_sha, job.fail_count) == (
        1,
        "momentum",
        "a",
        "sha",
        0,
    )
