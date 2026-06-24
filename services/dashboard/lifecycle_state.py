"""Per-group CERTIFICATION-LIFECYCLE state — the read side of the now-running within-day parity lifecycle.

Ben wants to SEE the staged progression every feature-group moves through as it earns trust:

    UNVERIFIED  ->  MONITORING  ->  CERTIFIED  ->  TRUSTED

read off three live Postgres tables the lifecycle writes (no schema change here — this is pure read side):

  * ``within_day_assignment`` — the disjoint one-owner-per-group lock (db/init/14). An ``active`` row means a
    subagent is currently MONITORING that group's live==backfill match; we surface WHO owns it.
  * ``within_day_parity_cert`` — the per-(feature,version,day) within-day stamp (db/init/13). A ``certified``
    row on a group's latest ``cert_day`` is the intraday-parity evidence ("reviewed, nothing outstanding"):
    we roll the group's features up to the group's CERTIFIED state with stable_cycles / value_rate / cert_day.
  * ``feature_trust`` — the permanent binary TRUSTED grant (db/init/12). A group whose features have ALL
    earned ``trust_state = 'TRUSTED'`` has reached the terminal TRUSTED stage.

Feature -> group is the REGISTRY catalog (``feature_grid._catalog_by_group``); ``within_day_parity_cert``
already carries ``group_name`` so its rows roll up directly. The whole snapshot is three small indexed
queries + a registry walk, cached upstream with a short TTL like the other dashboard read routes.

The lifecycle STAGE per group is the FURTHEST point it has reached, with the evidence behind each stage
preserved so the panel can show the staged story Ben described (intraday-ok -> pending-full-day -> trusted),
and which group each subagent currently owns.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import psycopg

from feature_grid import _catalog_by_group  # registry feature -> group map (read-side helper)

# The lifecycle stages, MOST-outstanding -> most-advanced. A group's reported stage is the furthest it has
# reached, EXCEPT a still-broken idle group is held at DIVERGENT (it failed a clean-day parity check and no
# owner/cert has lifted it since) so the panel separates "broken, needs a fix-it owner" from "never started".
STAGE_DIVERGENT = "divergent"
STAGE_UNVERIFIED = "unverified"
STAGE_MONITORING = "monitoring"
STAGE_CERTIFIED = "certified"
STAGE_TRUSTED = "trusted"
STAGE_ORDER: list[str] = [
    STAGE_DIVERGENT,
    STAGE_UNVERIFIED,
    STAGE_MONITORING,
    STAGE_CERTIFIED,
    STAGE_TRUSTED,
]

# feature_trust.lifecycle_state values the nightly clean-day sweep writes (quantlib/features/trust_lifecycle).
# DIVERGENT = the feature failed a clean-day parity check; this is the one we surface as a distinct stage so a
# broken-but-idle group stops hiding inside UNVERIFIED. The others are progress states the trust_state grant
# (read separately) already reflects, so we need only the DIVERGENT set here.
LIFECYCLE_DIVERGENT = "DIVERGENT"

# An assignment lock older than this with no heartbeat is treated as stale (matches the lifecycle's own
# dead-agent reclaim intent in db/init/14); we still show it, flagged stale, rather than as live MONITORING.
ASSIGNMENT_STALE_SECONDS = 30 * 60

# Short TTL cache: the lifecycle tables move on a minutes cadence (monitor cycles, nightly sweep), so a
# 20-second snapshot cache keeps a refresh cheap without ever looking stale to Ben.
_CACHE_TTL_SECONDS = 20.0
_cache: dict[str, object] = {}
_cache_at: float = 0.0


@dataclass
class AssignmentRow:
    """One ``within_day_assignment`` lock — which subagent currently owns (is MONITORING) a group."""

    group_name: str
    agent_id: str
    status: str
    claimed_at: str | None
    heartbeat_at: str | None
    released_at: str | None
    stale: bool


@dataclass
class CertRow:
    """The group-rolled ``within_day_parity_cert`` evidence for a group's LATEST cert_day — the intraday
    within-day verdict (status + stability + match rate) the CERTIFIED stage rests on."""

    group_name: str
    cert_day: str
    status: str  # certified | fix_pending | defected | skipped_unsettled | skipped_contaminated
    n_certified: int
    n_features_stamped: int
    stable_cycles: int  # MIN across the day's stamped features (the group is only as certified as its weakest)
    window_minutes: int
    value_rate: float | None  # WORST per-feature value_rate across the group's stamps
    reason: str | None


@dataclass
class GroupLifecycle:
    """One feature-group's place in the certification lifecycle: the furthest STAGE it has reached, plus the
    evidence behind each stage so the panel can tell the staged story."""

    group: str
    stage: str
    n_features: int
    n_trusted: int
    n_divergent: int  # features that failed a clean-day parity check (lifecycle_state DIVERGENT)
    fully_trusted: bool
    # MONITORING evidence (the active assignment lock), if any.
    owner: str | None
    owner_status: str | None
    owner_stale: bool
    # CERTIFIED evidence (the latest within-day cert roll-up), if any.
    cert_status: str | None
    cert_day: str | None
    cert_stable_cycles: int | None
    cert_window_minutes: int | None
    cert_value_rate: float | None
    cert_reason: str | None


def _connect() -> psycopg.Connection:
    # Reuse the exact DB connection contract every other dashboard DB read uses (env-driven, DB_PASSWORD
    # required) so this route fails the same way and needs no new config.
    from quantlib.features.validation_db import DB_KWARGS

    return psycopg.connect(**DB_KWARGS, connect_timeout=5)


def _iso(value: object) -> str | None:
    """A timestamp/date column -> ISO string (or None). Postgres hands back datetime/date objects."""
    if value is None:
        return None
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return str(value)


def read_assignments(conn: psycopg.Connection, now: datetime | None = None) -> list[AssignmentRow]:
    """The current assignment locks (newest claim first). An ``active`` lock with a heartbeat older than
    ASSIGNMENT_STALE_SECONDS is flagged ``stale`` so the panel can distinguish a live owner from a dead one."""
    now = now or datetime.now(timezone.utc)
    rows: list[AssignmentRow] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT group_name, agent_id, status, claimed_at, heartbeat_at, released_at "
            "FROM within_day_assignment ORDER BY claimed_at DESC"
        )
        for group_name, agent_id, status, claimed_at, heartbeat_at, released_at in cur.fetchall():
            stale = False
            if status == "active" and heartbeat_at is not None:
                age = (now - heartbeat_at).total_seconds()
                stale = age > ASSIGNMENT_STALE_SECONDS
            rows.append(
                AssignmentRow(
                    group_name=str(group_name),
                    agent_id=str(agent_id),
                    status=str(status),
                    claimed_at=_iso(claimed_at),
                    heartbeat_at=_iso(heartbeat_at),
                    released_at=_iso(released_at),
                    stale=stale,
                )
            )
    return rows


def read_latest_certs(conn: psycopg.Connection) -> dict[str, CertRow]:
    """The within-day cert evidence rolled up to one row per group, for each group's LATEST cert_day.

    A group's stamps for a day span its features; the group is only as certified as its WEAKEST feature, so
    we take the MIN stable_cycles and the WORST value_rate, count how many stamped features actually reached
    ``certified``, and adopt the dominant day status (certified only if EVERY stamped feature is certified).
    """
    by_group: dict[str, CertRow] = {}
    with conn.cursor() as cur:
        # Each group's most recent cert_day, then every stamp on that day for that group.
        cur.execute(
            """
            WITH latest AS (
                SELECT group_name, max(cert_day) AS cert_day
                FROM within_day_parity_cert
                GROUP BY group_name
            )
            SELECT c.group_name, c.cert_day, c.status, c.stable_cycles, c.window_minutes,
                   c.value_rate, c.reason
            FROM within_day_parity_cert c
            JOIN latest l ON l.group_name = c.group_name AND l.cert_day = c.cert_day
            """
        )
        accum: dict[str, list[tuple]] = {}
        for row in cur.fetchall():
            accum.setdefault(str(row[0]), []).append(row)

    for group_name, stamps in accum.items():
        cert_day = _iso(stamps[0][1])
        statuses = [str(stamp[2]) for stamp in stamps]
        n_certified = sum(1 for status in statuses if status == "certified")
        n_stamped = len(stamps)
        stable_values = [int(stamp[3]) for stamp in stamps]
        window_values = [int(stamp[4]) for stamp in stamps]
        rate_values = [float(stamp[5]) for stamp in stamps if stamp[5] is not None]
        reasons = [str(stamp[6]) for stamp in stamps if stamp[6]]
        # The group's day status: certified iff EVERY stamp is certified; else the worst non-certified status
        # present (fix_pending/defected dominate a skip so the panel surfaces the actionable state).
        if n_certified == n_stamped:
            group_status = "certified"
        else:
            severity = {"defected": 3, "fix_pending": 2, "skipped_contaminated": 1, "skipped_unsettled": 1}
            non_cert = [status for status in statuses if status != "certified"]
            group_status = max(non_cert, key=lambda status: severity.get(status, 0))
        by_group[group_name] = CertRow(
            group_name=group_name,
            cert_day=cert_day or "",
            status=group_status,
            n_certified=n_certified,
            n_features_stamped=n_stamped,
            stable_cycles=min(stable_values) if stable_values else 0,
            window_minutes=min(window_values) if window_values else 0,
            value_rate=min(rate_values) if rate_values else None,
            reason="; ".join(sorted(set(reasons))) if reasons else None,
        )
    return by_group


def read_trusted_features(conn: psycopg.Connection) -> set[str]:
    """{feature} that have earned the permanent binary TRUSTED grant (the terminal lifecycle stage)."""
    with conn.cursor() as cur:
        cur.execute("SELECT feature FROM feature_trust WHERE trust_state = 'TRUSTED'")
        return {str(row[0]) for row in cur.fetchall()}


def read_divergent_features(conn: psycopg.Connection) -> set[str]:
    """{feature} that FAILED a clean-day parity check (feature_trust.lifecycle_state = 'DIVERGENT') — broken,
    waiting on a fix. Surfaced so a stalled-and-divergent group is told apart from a never-started one."""
    with conn.cursor() as cur:
        cur.execute("SELECT feature FROM feature_trust WHERE lifecycle_state = %s", (LIFECYCLE_DIVERGENT,))
        return {str(row[0]) for row in cur.fetchall()}


def _active_owner(assignment: AssignmentRow | None) -> bool:
    """An assignment counts as a LIVE MONITORING owner only when it is ``active`` and not stale."""
    return assignment is not None and assignment.status == "active" and not assignment.stale


def build_group_lifecycles(
    catalog_by_group: dict[str, list[dict[str, object]]],
    assignments: list[AssignmentRow],
    certs: dict[str, CertRow],
    trusted_features: set[str],
    divergent_features: set[str] | None = None,
) -> list[GroupLifecycle]:
    """Assemble each registry group's furthest lifecycle STAGE + the evidence behind it. Pure (no DB), so the
    staged-progression logic is unit-tested without a database. Groups are returned trusted-last (the
    progression order: most work outstanding first), then alphabetically within a stage."""
    divergent_features = divergent_features or set()
    # Keep only the freshest assignment per group (read_assignments is newest-first).
    assignment_by_group: dict[str, AssignmentRow] = {}
    for assignment in assignments:
        assignment_by_group.setdefault(assignment.group_name, assignment)

    out: list[GroupLifecycle] = []
    for group in sorted(catalog_by_group):
        features = catalog_by_group[group]
        feature_names = [str(record["feature"]) for record in features]
        n_features = len(feature_names)
        n_trusted = sum(1 for name in feature_names if name in trusted_features)
        n_divergent = sum(1 for name in feature_names if name in divergent_features)
        fully_trusted = n_features > 0 and n_trusted == n_features

        assignment = assignment_by_group.get(group)
        cert = certs.get(group)

        # The stage is the FURTHEST point reached. TRUSTED is terminal; else CERTIFIED if the latest within-day
        # verdict is certified; else MONITORING if a live owner holds the lock; else DIVERGENT if the group has
        # a feature that failed a clean-day parity check and nothing has lifted it since (broken-and-idle, not
        # never-started); else UNVERIFIED.
        if fully_trusted:
            stage = STAGE_TRUSTED
        elif cert is not None and cert.status == "certified":
            stage = STAGE_CERTIFIED
        elif _active_owner(assignment):
            stage = STAGE_MONITORING
        elif n_divergent > 0:
            stage = STAGE_DIVERGENT
        else:
            stage = STAGE_UNVERIFIED

        out.append(
            GroupLifecycle(
                group=group,
                stage=stage,
                n_features=n_features,
                n_trusted=n_trusted,
                n_divergent=n_divergent,
                fully_trusted=fully_trusted,
                owner=assignment.agent_id if assignment is not None else None,
                owner_status=assignment.status if assignment is not None else None,
                owner_stale=assignment.stale if assignment is not None else False,
                cert_status=cert.status if cert is not None else None,
                cert_day=cert.cert_day if cert is not None else None,
                cert_stable_cycles=cert.stable_cycles if cert is not None else None,
                cert_window_minutes=cert.window_minutes if cert is not None else None,
                cert_value_rate=cert.value_rate if cert is not None else None,
                cert_reason=cert.reason if cert is not None else None,
            )
        )

    out.sort(key=lambda item: (STAGE_ORDER.index(item.stage), item.group))
    return out


def summarize(groups: list[GroupLifecycle]) -> dict[str, int]:
    """Per-stage group counts for the panel header (how many groups sit at each lifecycle stage)."""
    counts = {stage: 0 for stage in STAGE_ORDER}
    for group in groups:
        counts[group.stage] += 1
    return counts


def _build_snapshot() -> dict[str, object]:
    catalog_by_group = _catalog_by_group()
    with _connect() as conn:
        assignments = read_assignments(conn)
        certs = read_latest_certs(conn)
        trusted_features = read_trusted_features(conn)
        divergent_features = read_divergent_features(conn)
    groups = build_group_lifecycles(
        catalog_by_group, assignments, certs, trusted_features, divergent_features
    )
    total_features = sum(group.n_features for group in groups)
    total_trusted = sum(group.n_trusted for group in groups)
    total_divergent = sum(group.n_divergent for group in groups)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage_order": STAGE_ORDER,
        "summary": summarize(groups),
        "n_groups": len(groups),
        "n_features": total_features,
        "n_trusted_features": total_trusted,
        "n_divergent_features": total_divergent,
        "active_owners": [
            asdict(assignment) for assignment in assignments if _active_owner(assignment)
        ],
        "groups": [asdict(group) for group in groups],
    }


def lifecycle_snapshot() -> dict[str, object]:
    """The full lifecycle-state snapshot the ``/api/lifecycle-state`` route serves, short-TTL cached so a
    refresh is one cheap set of indexed queries off the request path most of the time."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache and (now - _cache_at) < _CACHE_TTL_SECONDS:
        return dict(_cache)
    snapshot = _build_snapshot()
    _cache = snapshot
    _cache_at = now
    return snapshot
