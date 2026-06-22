"""WDPC continuous-deployment — the FIFO deploy QUEUE CRUD (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md
§5.2). A tested + in-scope per-group fix (§4) is ENQUEUED here; the ONE serialized applier
(within_day_applier.py) dequeues by ``enqueued_at`` (FIFO), one job at a time.

This mirrors within_day_assignment's style EXACTLY: pure parameterized SQL against ``within_day_deploy_queue``
(db/init/14_wdpc_assignment.sql), a thin ``_execute`` writer, and ``dry_run`` the DEFAULT on every state
mutation so the build/test path never writes the live DB — live activation is the Lead's gated step.

The queue is a plain FIFO of INDEPENDENT jobs (no merge logic) because assignment is disjoint (§5.1): two
agents never touch the same group's code, so two queued jobs never conflict. State machine:

    queued --claim_next--> applying --mark_applied------> applied      (tripwire confirmed live==backfill)
                                     --mark_rolled_back--> rolled_back  (post-swap tripwire FAILED, reverted)
                                     --mark_escalated----> escalated    (scope-guard refused / shared-code)
                                     --mark_failed-------> failed        (transient; re-enqueue with backoff)

``mark_failed`` bumps ``fail_count`` so a group that repeatedly fails its tripwire (§5.4 starvation guard)
stops auto-retrying and is escalated by the applier once ``fail_count >= MAX_FAIL_COUNT``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("within_day_deploy_queue")

MAX_FAIL_COUNT = 3  # a group that fails its tripwire this many times stops auto-retrying → escalate (§5.4)

STATUS_QUEUED = "queued"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ESCALATED = "escalated"
STATUS_FAILED = "failed"

_TERMINAL_STATUSES = {STATUS_APPLIED, STATUS_ROLLED_BACK, STATUS_ESCALATED}


@dataclass
class QueuedJob:
    """One dequeued FIFO job — the row the applier acts on (mirrors within_day_applier.DeployJob's fields,
    plus the queue bookkeeping the applier reads for the backoff/starvation decision)."""

    job_id: int
    group_name: str
    agent_id: str
    commit_sha: str
    fail_count: int


_ENQUEUE = """
INSERT INTO within_day_deploy_queue (group_name, agent_id, commit_sha, enqueued_at, status)
VALUES (%(group_name)s, %(agent_id)s, %(commit_sha)s, now(), 'queued')
RETURNING id
"""

# Claim the OLDEST queued job (FIFO by enqueued_at) and flip it to 'applying' atomically, so a second applier
# (defence-in-depth — there should only ever be one) can never grab the same row. SKIP LOCKED keeps the claim
# non-blocking under the single-applier contract.
_CLAIM_NEXT = """
UPDATE within_day_deploy_queue SET status='applying', started_at=now()
WHERE id = (
    SELECT id FROM within_day_deploy_queue
    WHERE status='queued'
    ORDER BY enqueued_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, group_name, agent_id, commit_sha, fail_count
"""

_MARK_TERMINAL = """
UPDATE within_day_deploy_queue SET status=%(status)s, finished_at=now(), detail=%(detail)s
WHERE id = %(job_id)s AND status='applying'
RETURNING id
"""

# A transient failure: bump fail_count and return the job to the back of the FIFO ('queued') so it retries
# AFTER everything currently waiting (no starvation of other groups). enqueued_at is refreshed so the retry
# sorts last. The applier escalates instead of calling this once fail_count would reach MAX_FAIL_COUNT.
_MARK_FAILED_REQUEUE = """
UPDATE within_day_deploy_queue
SET status='queued', fail_count=fail_count + 1, started_at=NULL, enqueued_at=now(), detail=%(detail)s
WHERE id = %(job_id)s AND status='applying'
RETURNING id, fail_count
"""

_PENDING = """
SELECT id, group_name, agent_id, commit_sha, fail_count
FROM within_day_deploy_queue WHERE status='queued' ORDER BY enqueued_at ASC
"""


def _execute(sql: str, params: dict[str, object]) -> list[tuple]:
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
    return rows


def enqueue(group_name: str, agent_id: str, commit_sha: str, *, dry_run: bool = True) -> int | None:
    """Enqueue a tested, in-scope fix for ``group_name`` (commit ``commit_sha``, owned by ``agent_id``).
    Returns the new job id (None in dry_run). The fix MUST already pass the scope-guard (§4) before it is
    enqueued — the applier re-checks defence-in-depth, but the queue itself holds no policy."""
    params: dict[str, object] = {"group_name": group_name, "agent_id": agent_id, "commit_sha": commit_sha}
    if dry_run:
        logger.info(
            "DRY-RUN enqueue group=%s agent=%s commit=%s (no DB write)", group_name, agent_id, commit_sha
        )
        return None
    rows = _execute(_ENQUEUE, params)
    return int(rows[0][0]) if rows else None


def claim_next(*, dry_run: bool = True) -> QueuedJob | None:
    """Atomically claim the OLDEST queued job (FIFO) and flip it to 'applying'. Returns the job, or None if
    the queue is empty. The single serialized applier calls this each loop. dry_run returns None (nothing to
    apply in a build/test run; tests inject a fake dequeue)."""
    if dry_run:
        logger.info("DRY-RUN claim_next (no DB read/write)")
        return None
    rows = _execute(_CLAIM_NEXT, {})
    if not rows:
        return None
    job_id, group_name, agent_id, commit_sha, fail_count = rows[0]
    return QueuedJob(
        job_id=int(job_id),
        group_name=str(group_name),
        agent_id=str(agent_id),
        commit_sha=str(commit_sha),
        fail_count=int(fail_count),
    )


def _mark_terminal(job_id: int, status: str, detail: str, *, dry_run: bool) -> bool:
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"_mark_terminal called with non-terminal status {status!r}")
    if dry_run:
        logger.info("DRY-RUN mark %s job=%d detail=%s (no DB write)", status, job_id, detail)
        return True
    return bool(_execute(_MARK_TERMINAL, {"job_id": job_id, "status": status, "detail": detail}))


def mark_applied(job_id: int, detail: str = "", *, dry_run: bool = True) -> bool:
    """Mark a job 'applied' — the hot-swap landed and the bus tripwire confirmed live==backfill (§5.2)."""
    return _mark_terminal(job_id, STATUS_APPLIED, detail, dry_run=dry_run)


def mark_rolled_back(job_id: int, detail: str = "", *, dry_run: bool = True) -> bool:
    """Mark a job 'rolled_back' — the swap landed but the post-swap tripwire FAILED, so the applier reverted
    that one group's swap (§5.4 case 2). Terminal; the Lead reviews."""
    return _mark_terminal(job_id, STATUS_ROLLED_BACK, detail, dry_run=dry_run)


def mark_escalated(job_id: int, detail: str = "", *, dry_run: bool = True) -> bool:
    """Mark a job 'escalated' — the scope-guard refused it (shared-code / fingerprint / trusted-perturbation,
    §4) or it exhausted its retry budget. Terminal; goes to the Lead/human (§5.4 case 1)."""
    return _mark_terminal(job_id, STATUS_ESCALATED, detail, dry_run=dry_run)


def mark_failed(job_id: int, fail_count: int, detail: str = "", *, dry_run: bool = True) -> str:
    """Handle a TRANSIENT failure with the §5.4 backoff: if this failure would reach ``MAX_FAIL_COUNT``,
    ESCALATE (stop auto-retrying — the starvation guard); otherwise re-enqueue at the back of the FIFO with
    ``fail_count`` bumped. Returns the resulting status ('escalated' | 'queued'). ``fail_count`` is the job's
    CURRENT count (from the claimed QueuedJob); the decision is purely a function of it, so this is testable
    without a DB."""
    if fail_count + 1 >= MAX_FAIL_COUNT:
        escalate_detail = f"{detail} (exhausted {MAX_FAIL_COUNT} retries)".strip()
        _mark_terminal(job_id, STATUS_ESCALATED, escalate_detail, dry_run=dry_run)
        return STATUS_ESCALATED
    if dry_run:
        logger.info(
            "DRY-RUN mark_failed job=%d (fail_count %d→%d, re-enqueue) detail=%s (no DB write)",
            job_id,
            fail_count,
            fail_count + 1,
            detail,
        )
        return STATUS_QUEUED
    _execute(_MARK_FAILED_REQUEUE, {"job_id": job_id, "detail": detail})
    return STATUS_QUEUED


def pending(*, dry_run: bool = True) -> list[QueuedJob]:
    """The current FIFO-ordered backlog (status='queued'). Read-only; for the dashboard / a dry-run peek."""
    if dry_run:
        logger.info("DRY-RUN pending (no DB read)")
        return []
    return [
        QueuedJob(
            job_id=int(row[0]),
            group_name=str(row[1]),
            agent_id=str(row[2]),
            commit_sha=str(row[3]),
            fail_count=int(row[4]),
        )
        for row in _execute(_PENDING, {})
    ]
