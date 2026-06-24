"""THE PRIORITY QUEUE behind the feature-worker fleet — the ordering function that decides which group a
free worker picks next (docs/FEATURE_WORKER_FLEET.md §3).

The "queue" is not a new table: it is the set of feature-GROUPS that still need lifecycle work, DERIVED from
the three tables the within-day lifecycle already writes (the same ones the dashboard's
:mod:`services.dashboard.lifecycle_state` reads), plus the disjoint assignment lock:

  * ``feature_parity_defect`` (db/init/10) — an OPEN row means the group is DIVERGENT (live != backfill on a
    clean day): the highest-priority work, a root-cause + fix.
  * ``within_day_parity_cert`` (db/init/13) — a ``certified`` row on the group's latest cert_day is the
    intraday within-day evidence; absence means the group is still UNVERIFIED (needs monitoring).
  * ``feature_trust`` (db/init/12) — a group whose features have ALL earned ``trust_state='TRUSTED'`` is DONE
    and drops out of the queue; a certified-but-not-yet-trusted group is the lowest-priority tail.
  * ``within_day_assignment`` (db/init/14) — the disjoint one-owner-per-group lock. A group with a LIVE
    (active, non-stale) lock held by ANOTHER agent is already being worked, so it is excluded from the queue
    a free worker sees — that exclusion is what makes the fleet conflict-free without any coordination.

Feature -> group is the REGISTRY catalog (the same map the dashboard uses). The ordering is a pure function
over the four reads (:func:`order_groups`), so the priority logic is unit-tested with no database. The DB
round-trip (:func:`next_group` / :func:`queue_snapshot`) is a thin shell that fetches the four states and
hands them to the pure ordering.

PRIORITY (lowest ``QueuePriority`` value = picked first):

    DIVERGENT (0)  ->  UNVERIFIED (1)  ->  MONITORING_STALE (2)  ->  CERTIFIED_PENDING_TRUST (3)

A free worker calls :func:`next_group`, claims it via the assignment lock, advances it one phase
(:mod:`within_day_run`), releases, and loops — see :mod:`quantlib.features.feature_worker`.

⭐ SAFETY: every function here is READ-ONLY. The queue never writes a table; claiming/advancing is the
worker's job and stays ``dry_run`` by default. ``dry_run`` short-circuits the DB reads so the ordering is
exercisable fully offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum

import psycopg

from quantlib.features.registry import REGISTRY
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("feature_queue")

# An assignment lock this old with no heartbeat is treated as stale (matches within_day_assignment's
# DEFAULT_HEARTBEAT_TIMEOUT_S and the dashboard's ASSIGNMENT_STALE_SECONDS): a dead agent must not keep a
# group out of the queue forever. A stale-locked group is re-offered (as MONITORING_STALE) for reclaim.
ASSIGNMENT_STALE_SECONDS = 600


class QueuePriority(IntEnum):
    """Lifecycle-phase priority — the order a free worker picks groups. Lower = picked first."""

    DIVERGENT = 0  # OPEN parity defect: root-cause + fix (the most urgent — live is wrong)
    UNVERIFIED = 1  # never certified, free to claim: run the monitor to certify
    MONITORING_STALE = 2  # a dead agent left an active lock: reclaim + re-monitor
    CERTIFIED_PENDING_TRUST = 3  # certified within-day, awaiting the nightly trust grant: lowest


@dataclass(frozen=True)
class QueueItem:
    """One enqueued feature-group: its lifecycle phase (priority) and the evidence behind the ordering. A
    free worker takes the lowest-priority item it can claim."""

    group_name: str
    priority: QueuePriority
    n_features: int
    n_trusted: int
    n_open_defects: int
    cert_status: str | None  # latest within_day_parity_cert group status, if any
    cert_day: str | None
    owner: str | None  # the agent_id holding a (possibly stale) lock, if any
    owner_stale: bool

    @property
    def phase(self) -> str:
        return self.priority.name


def _group_of_feature() -> dict[str, str]:
    """{feature -> owning group} from the registry catalog (the same source the dashboard uses)."""
    catalog = REGISTRY.catalog()
    return {str(row["feature"]): str(row["group"]) for row in catalog.iter_rows(named=True)}


def _group_feature_counts() -> dict[str, int]:
    """{group -> declared feature count} — the denominator for 'fully trusted'."""
    counts: dict[str, int] = {}
    for group in REGISTRY.groups():
        counts[group.name] = len(group.declare())
    return counts


def order_groups(
    feature_counts: dict[str, int],
    trusted_features_by_group: dict[str, int],
    open_defect_groups: dict[str, int],
    cert_status_by_group: dict[str, tuple[str, str | None]],
    active_locks: dict[str, tuple[str, bool]],
) -> list[QueueItem]:
    """The PURE ordering function — no DB. Given the four lifecycle states, return the groups that still need
    work, ordered by lifecycle phase (DIVERGENT first), then alphabetically within a phase for stable output.

    Args:
        feature_counts: {group -> # declared features} (the registry universe of groups).
        trusted_features_by_group: {group -> # of its features with trust_state='TRUSTED'}.
        open_defect_groups: {group -> # OPEN feature_parity_defect rows} (the DIVERGENT signal).
        cert_status_by_group: {group -> (latest cert status, cert_day)} from within_day_parity_cert.
        active_locks: {group -> (agent_id, is_stale)} for groups with an 'active' assignment lock.

    A group is EXCLUDED from the queue when it is either fully trusted (nothing to do) OR held by a LIVE
    (active, non-stale) lock owned by another agent (already being worked — the conflict-free exclusion).
    """
    items: list[QueueItem] = []
    for group_name in sorted(feature_counts):
        n_features = feature_counts[group_name]
        n_trusted = trusted_features_by_group.get(group_name, 0)
        n_open = open_defect_groups.get(group_name, 0)
        cert_status, cert_day = cert_status_by_group.get(group_name, (None, None))
        lock = active_locks.get(group_name)
        owner = lock[0] if lock is not None else None
        owner_stale = lock[1] if lock is not None else False
        live_lock = lock is not None and not owner_stale

        # Fully trusted -> done, off the queue. (n_features==0 can't be "all trusted".)
        if n_features > 0 and n_trusted == n_features:
            continue

        # A live (non-stale) lock means another worker owns it right now: exclude so two workers never
        # contend for one group. (A STALE lock is NOT excluded — it is re-offered for reclaim below.)
        if live_lock:
            continue

        if n_open > 0:
            priority = QueuePriority.DIVERGENT
        elif owner_stale:
            priority = QueuePriority.MONITORING_STALE
        elif cert_status == "certified":
            priority = QueuePriority.CERTIFIED_PENDING_TRUST
        else:
            priority = QueuePriority.UNVERIFIED

        items.append(
            QueueItem(
                group_name=group_name,
                priority=priority,
                n_features=n_features,
                n_trusted=n_trusted,
                n_open_defects=n_open,
                cert_status=cert_status,
                cert_day=cert_day,
                owner=owner,
                owner_stale=owner_stale,
            )
        )

    items.sort(key=lambda item: (int(item.priority), item.group_name))
    return items


def _read_states(conn: psycopg.Connection, now: datetime) -> tuple[
    dict[str, int],
    dict[str, int],
    dict[str, tuple[str, str | None]],
    dict[str, tuple[str, bool]],
]:
    """Fetch the four lifecycle states from the live tables (one small indexed query each)."""
    feature_to_group = _group_of_feature()

    with conn.cursor() as cur:
        # 1. trusted feature count per group (feature_trust -> registry group map).
        cur.execute("SELECT feature FROM feature_trust WHERE trust_state = 'TRUSTED'")
        trusted_by_group: dict[str, int] = {}
        for (feature,) in cur.fetchall():
            group = feature_to_group.get(str(feature))
            if group is not None:
                trusted_by_group[group] = trusted_by_group.get(group, 0) + 1

        # 2. OPEN parity defects per group (the DIVERGENT signal). feature_group is carried on the row.
        cur.execute(
            "SELECT feature, feature_group FROM feature_parity_defect "
            "WHERE status IN ('open', 'investigating')"
        )
        open_defect_groups: dict[str, int] = {}
        for feature, feature_group in cur.fetchall():
            group = str(feature_group) if feature_group else feature_to_group.get(str(feature))
            if group is not None:
                open_defect_groups[group] = open_defect_groups.get(group, 0) + 1

        # 3. each group's latest within-day cert status (certified iff EVERY stamp that day is certified).
        cur.execute(
            """
            WITH latest AS (
                SELECT group_name, max(cert_day) AS cert_day
                FROM within_day_parity_cert
                GROUP BY group_name
            )
            SELECT c.group_name, c.cert_day,
                   bool_and(c.status = 'certified') AS all_certified
            FROM within_day_parity_cert c
            JOIN latest l ON l.group_name = c.group_name AND l.cert_day = c.cert_day
            GROUP BY c.group_name, c.cert_day
            """
        )
        cert_status_by_group: dict[str, tuple[str, str | None]] = {}
        for group_name, cert_day, all_certified in cur.fetchall():
            status = "certified" if all_certified else "pending"
            day_iso = cert_day.isoformat() if cert_day is not None else None
            cert_status_by_group[str(group_name)] = (status, day_iso)

        # 4. active assignment locks (with staleness from the heartbeat age).
        cur.execute(
            "SELECT group_name, agent_id, heartbeat_at FROM within_day_assignment WHERE status = 'active'"
        )
        active_locks: dict[str, tuple[str, bool]] = {}
        for group_name, agent_id, heartbeat_at in cur.fetchall():
            stale = heartbeat_at is None or (now - heartbeat_at).total_seconds() > ASSIGNMENT_STALE_SECONDS
            active_locks[str(group_name)] = (str(agent_id), stale)

    return trusted_by_group, open_defect_groups, cert_status_by_group, active_locks


def queue_snapshot(*, dry_run: bool = True) -> list[QueueItem]:
    """The full priority-ordered queue a free worker (or the dashboard) sees. READ-ONLY. ``dry_run`` returns
    an empty queue without touching the DB (the offline default — tests drive :func:`order_groups`)."""
    if dry_run:
        logger.info("DRY-RUN queue_snapshot (no DB read) — returning empty queue")
        return []
    feature_counts = _group_feature_counts()
    now = datetime.now(timezone.utc)
    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn:
        trusted, defects, certs, locks = _read_states(conn, now)
    return order_groups(feature_counts, trusted, defects, certs, locks)


def next_group(*, dry_run: bool = True) -> QueueItem | None:
    """The single highest-priority claimable group (the head of the queue), or None if every group is either
    trusted or live-locked by another worker. READ-ONLY — the caller does the claim."""
    queue = queue_snapshot(dry_run=dry_run)
    return queue[0] if queue else None
