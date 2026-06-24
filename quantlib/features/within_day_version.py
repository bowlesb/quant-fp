"""WDPC continuous-deployment — VERSION-AWARENESS: "is the deployed version the one trust was earned on?"
(docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §2/§3, Ben's "a subagent can see its new version reached prod").

Trust is keyed by (feature, version) AND records the PROVENANCE the grant was earned at — ``trusted_content_hash``
(the blake2b of the group's compute SOURCE, :func:`trust_policy.group_content_hash`) and ``trusted_git_commit``
(:func:`trust_policy.current_git_commit`). The human ``version`` label can stay fixed across a fingerprint-neutral
compute correction (a WDPC hot-swap, §3), but the CONTENT HASH changes the instant the deployed code changes.

That gives two signals this module exposes:

  1. ``version_status(group)`` — compare the LIVE-RUNNING group's content_hash to the trust grant's
     ``trusted_content_hash``. ``LIVE_MATCHES_TRUST`` means "the deployed version IS the one trust was earned
     on" — the all-clear a subagent looks for to know its fix reached prod under a still-valid grant.
     ``LIVE_DIVERGED`` means the deployed code changed since the grant → trust must RESET (re-earn under the
     new content hash). ``UNTRUSTED`` / ``NOT_REGISTERED`` are the trivial cases.

  2. ``reset_trust_on_content_change(group)`` — when a group's content hash diverged from its grant, flip its
     features back to NON_TRUSTED so they re-earn under the new code. This is the lifecycle's version-reset:
     a code change invalidates the old grant; the within-day monitor then re-certifies the new content hash.

PURE where it counts: ``compare_status`` decides over already-fetched (live_hash, grant_hash) pairs with NO DB,
so it is fully unit-testable. The DB reads/writes default to ``dry_run`` — the live reset is the Lead's gated
step, exactly like the assignment lock and the trust grant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import psycopg

from quantlib.features.base import FeatureGroup
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_policy import current_git_commit, group_content_hash
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("within_day_version")


class VersionStatus(Enum):
    """Whether the LIVE-running code is the one a feature's trust was earned on."""

    LIVE_MATCHES_TRUST = "live_matches_trust"  # deployed content hash == grant's → trust still valid
    LIVE_DIVERGED = "live_diverged"  # deployed content hash != grant's → trust must reset
    UNTRUSTED = "untrusted"  # the feature is NON_TRUSTED → nothing to compare
    NOT_REGISTERED = "not_registered"  # no trust grant row at all → never earned


@dataclass
class FeatureVersionReport:
    """Per-feature version-awareness verdict (the signal a subagent reads to know its version reached prod)."""

    feature: str
    version: str
    status: VersionStatus
    live_content_hash: str
    trusted_content_hash: str | None
    live_git_commit: str | None
    trusted_git_commit: str | None


_SELECT_TRUST = """
SELECT feature, version, trust_state, trusted_content_hash, trusted_git_commit
FROM feature_trust WHERE feature = ANY(%(features)s)
"""

# Reset every feature of a group back to NON_TRUSTED so it re-earns under the new content hash. We only flip
# rows that are currently TRUSTED for a DIFFERENT content hash than the one now live — a no-op when the live
# code already matches the grant (idempotent), and it never touches an already-NON_TRUSTED row.
_RESET_TRUST = """
UPDATE feature_trust
SET trust_state='NON_TRUSTED', untrusted_at=now(), updated_at=now()
WHERE feature = ANY(%(features)s)
  AND trust_state='TRUSTED'
  AND trusted_content_hash IS DISTINCT FROM %(live_content_hash)s
RETURNING feature
"""


def compare_status(
    live_content_hash: str,
    trust_state: str | None,
    trusted_content_hash: str | None,
) -> VersionStatus:
    """The PURE core: given the live content hash and the grant's (trust_state, trusted_content_hash), decide
    the version status. No DB, no registry — directly unit-testable.

    NON_TRUSTED / no row → trivial; TRUSTED with a matching hash → the all-clear; TRUSTED with a differing
    (or absent) hash → diverged (the deployed code is not what trust was earned on)."""
    if trust_state is None:
        return VersionStatus.NOT_REGISTERED
    if trust_state != "TRUSTED":
        return VersionStatus.UNTRUSTED
    if trusted_content_hash is not None and trusted_content_hash == live_content_hash:
        return VersionStatus.LIVE_MATCHES_TRUST
    return VersionStatus.LIVE_DIVERGED


def _features_of(group: FeatureGroup) -> list[str]:
    return [spec.name for spec in group.declare()]


def version_status(group: FeatureGroup, *, dry_run: bool = True) -> list[FeatureVersionReport]:
    """For each feature of ``group``, report whether the LIVE-running content hash matches its trust grant's.

    The live content hash is the SAME machine-derived digest the trust grant records
    (:func:`group_content_hash`), computed off the group instance currently in ``REGISTRY`` — so after a
    hot-swap re-imports the group, this reflects the NEW deployed code. dry_run reports against an EMPTY
    trust table (every feature NOT_REGISTERED) so the build/test path opens no DB connection."""
    live_hash = group_content_hash(group)
    live_commit = current_git_commit()
    features = _features_of(group)

    trust_by_feature: dict[str, tuple[str | None, str | None, str | None]] = {}
    if not dry_run:
        with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
            cur.execute(_SELECT_TRUST, {"features": features})
            for feature, version, trust_state, trusted_hash, trusted_commit in cur.fetchall():
                trust_by_feature[feature] = (trust_state, trusted_hash, trusted_commit)
    else:
        logger.info("DRY-RUN version_status group=%s (no DB read; all NOT_REGISTERED)", group.name)

    reports: list[FeatureVersionReport] = []
    for feature in features:
        trust_state, trusted_hash, trusted_commit = trust_by_feature.get(feature, (None, None, None))
        reports.append(
            FeatureVersionReport(
                feature=feature,
                version=group.version,
                status=compare_status(live_hash, trust_state, trusted_hash),
                live_content_hash=live_hash,
                trusted_content_hash=trusted_hash,
                live_git_commit=live_commit,
                trusted_git_commit=trusted_commit,
            )
        )
    return reports


def is_deployed_version_trusted(group: FeatureGroup, *, dry_run: bool = True) -> bool:
    """The one-line signal the monitor/applier reads: True iff EVERY trusted feature of the group is trusted
    on the LIVE-running content hash (no LIVE_DIVERGED). A group with only untrusted/unregistered features is
    trivially True (nothing trusted to invalidate). After a hot-swap this goes False until re-certified."""
    return not any(
        report.status is VersionStatus.LIVE_DIVERGED
        for report in version_status(group, dry_run=dry_run)
    )


def is_group_untrusted(group_name: str, *, dry_run: bool = True) -> bool:
    """True iff NO feature of ``group_name`` is currently TRUSTED — the §7 safety predicate the hot-swap
    scope-guard reads (an untrusted feature is never consumed by a live strategy, so an in-flight value change
    from a swap can't affect a trade). Conservative + fail-closed for the swap: dry_run returns True (the
    build/test path has no trust table → treat as untrusted, the SAFE direction for a swap that only deploys
    untrusted fixes). A group not registered → True (nothing trusted)."""
    if dry_run:
        logger.info("DRY-RUN is_group_untrusted group=%s → True (no DB read)", group_name)
        return True
    group = group_by_name(group_name)
    if group is None:
        return True
    features = _features_of(group)
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_SELECT_TRUST, {"features": features})
        return not any(row[2] == "TRUSTED" for row in cur.fetchall())


def reset_trust_on_content_change(group: FeatureGroup, *, dry_run: bool = True) -> list[str]:
    """RESET trust for every feature of ``group`` whose grant was earned on a DIFFERENT content hash than the
    one now live — i.e. the deployed code changed (a hot-swap landed) so the old grant no longer applies; the
    features must re-earn trust under the new content hash. Returns the features reset.

    Idempotent: a feature whose live hash already matches its grant is untouched (the SQL only flips TRUSTED
    rows with a distinct content hash). dry_run reports what WOULD reset (every currently-diverged feature)
    without a DB write — the live reset is the Lead's gated step."""
    live_hash = group_content_hash(group)
    features = _features_of(group)

    if dry_run:
        would_reset = [
            report.feature
            for report in version_status(group, dry_run=True)
            if report.status is VersionStatus.LIVE_DIVERGED
        ]
        logger.info(
            "DRY-RUN reset_trust_on_content_change group=%s live_hash=%s would-reset=%s (no DB write)",
            group.name,
            live_hash,
            would_reset,
        )
        return would_reset

    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_RESET_TRUST, {"features": features, "live_content_hash": live_hash})
        reset = [row[0] for row in cur.fetchall()]
        conn.commit()
    if reset:
        logger.info(
            "RESET trust for %d feature(s) of group=%s on content change (live_hash=%s): %s",
            len(reset),
            group.name,
            live_hash,
            reset,
        )
    return reset


def group_by_name(group_name: str) -> FeatureGroup | None:
    """Look up the LIVE-registered group instance by name (the same lookup the hot-swap mutates). After a
    hot-swap re-imports the group, this returns the NEW instance — so version_status off it reflects the
    deployed code. Returns None if the name isn't registered."""
    for group in REGISTRY.groups():
        if group.name == group_name:
            return group
    return None
