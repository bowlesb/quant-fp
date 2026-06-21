"""WDPC continuous-deployment — the disjoint ASSIGNMENT lock (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md
§5.1). Each subagent owns EXACTLY ONE group via ``within_day_assignment`` (PK ``group_name``), so two agents
never touch the same code — the conflict-preventer the whole CD system leans on.

Pure DB-lock operations: claim (INSERT, PK blocks a double-claim), heartbeat (liveness), release (on done),
and reclaim (a stale-heartbeat lock times out so a dead agent never holds a group forever). The Lead/ordering
query assigns; the applier never deploys a group whose lock isn't held by the submitting agent. ``dry_run`` is
the default — the live activation (real DB writes) is the Lead's gated step.
"""

from __future__ import annotations

import logging

import psycopg

from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("within_day_assignment")

DEFAULT_HEARTBEAT_TIMEOUT_S = 600  # a lock whose heartbeat is older than this is reclaimable (dead agent)

_CLAIM = """
INSERT INTO within_day_assignment (group_name, agent_id, claimed_at, heartbeat_at, status)
VALUES (%(group_name)s, %(agent_id)s, now(), now(), 'active')
ON CONFLICT (group_name) DO UPDATE SET
  agent_id=EXCLUDED.agent_id, claimed_at=now(), heartbeat_at=now(), status='active', released_at=NULL
WHERE within_day_assignment.status <> 'active'
   OR within_day_assignment.heartbeat_at < now() - (%(timeout_s)s || ' seconds')::interval
RETURNING agent_id
"""

_HEARTBEAT = """
UPDATE within_day_assignment SET heartbeat_at = now()
WHERE group_name = %(group_name)s AND agent_id = %(agent_id)s AND status = 'active'
RETURNING group_name
"""

_RELEASE = """
UPDATE within_day_assignment SET status='released', released_at=now()
WHERE group_name = %(group_name)s AND agent_id = %(agent_id)s AND status='active'
RETURNING group_name
"""

_RECLAIM_STALE = """
UPDATE within_day_assignment SET status='timed_out'
WHERE status='active' AND heartbeat_at < now() - (%(timeout_s)s || ' seconds')::interval
RETURNING group_name
"""


def _execute(sql: str, params: dict[str, object]) -> list[tuple]:
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
    return rows


def claim(
    group_name: str, agent_id: str, *, timeout_s: int = DEFAULT_HEARTBEAT_TIMEOUT_S, dry_run: bool = True
) -> bool:
    """Claim ``group_name`` for ``agent_id``. Returns True if claimed (free, released, or a timed-out lock),
    False if another agent holds a LIVE lock. dry_run logs the intent + returns True without a DB write."""
    params: dict[str, object] = {"group_name": group_name, "agent_id": agent_id, "timeout_s": timeout_s}
    if dry_run:
        logger.info("DRY-RUN claim group=%s agent=%s (no DB write)", group_name, agent_id)
        return True
    return bool(_execute(_CLAIM, params))


def heartbeat(group_name: str, agent_id: str, *, dry_run: bool = True) -> bool:
    """Bump the lock's heartbeat (liveness). Returns True if the agent still holds the active lock."""
    params: dict[str, object] = {"group_name": group_name, "agent_id": agent_id}
    if dry_run:
        logger.info("DRY-RUN heartbeat group=%s agent=%s (no DB write)", group_name, agent_id)
        return True
    return bool(_execute(_HEARTBEAT, params))


def release(group_name: str, agent_id: str, *, dry_run: bool = True) -> bool:
    """Release the lock (on certify/done). Returns True if it was the agent's active lock."""
    params: dict[str, object] = {"group_name": group_name, "agent_id": agent_id}
    if dry_run:
        logger.info("DRY-RUN release group=%s agent=%s (no DB write)", group_name, agent_id)
        return True
    return bool(_execute(_RELEASE, params))


def reclaim_stale(*, timeout_s: int = DEFAULT_HEARTBEAT_TIMEOUT_S, dry_run: bool = True) -> list[str]:
    """Time out every active lock whose heartbeat is older than ``timeout_s`` (dead-agent reclaim). Returns
    the reclaimed group names so the ordering query can re-assign them."""
    if dry_run:
        logger.info("DRY-RUN reclaim_stale timeout=%ds (no DB write)", timeout_s)
        return []
    return [row[0] for row in _execute(_RECLAIM_STALE, {"timeout_s": timeout_s})]
