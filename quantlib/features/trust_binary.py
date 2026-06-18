"""Binary trust grading (docs/TRUST_REDESIGN.md).

A ``(feature, version)`` is TRUSTED or NON_TRUSTED. Trust is EARNED — deterministic-by-construction
(CALENDAR, parity guaranteed) or stream==backfill within the feature's tolerance on >= one CLEAN day —
and PERMANENT: once TRUSTED at a version it is never auto-demoted (a code change bumps the version, which
earns trust on its own; only an explicit random-check failure flips it back). Every grant records the
provenance to REPLAY the verdict (day, version, git commit, content hash) and appends a
``feature_trust_check`` row.

Pure row-builders over the registry + the day's clean history; a thin psycopg writer mirrors
``validation_db`` / ``trust_lifecycle``. The grading is contamination-aware by construction: it consumes
the same CLEAN per-(feature, day) history the lifecycle used, so a capture-contaminated day never earns
(or condemns) trust.
"""

from __future__ import annotations

import polars as pl
import psycopg

from quantlib.features.base import FeatureType
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_policy import (
    TrustTolerance,
    current_git_commit,
    group_content_hash,
    policy_for,
)
from quantlib.features.validation_db import DB_KWARGS

TRUSTED = "TRUSTED"
NON_TRUSTED = "NON_TRUSTED"


def feature_policy_map() -> dict[str, tuple[str, TrustTolerance]]:
    """feature -> (version, trust-tolerance policy). The policy carries the rtol, min_pass_rate, and the
    deterministic / full_universe flags the grading and the cell comparison both read."""
    return {
        spec.name: (group.version, policy_for(group.type, spec.tolerance))
        for group, spec in REGISTRY.feature_specs()
    }


def cell_tolerance_map() -> dict[str, float]:
    """feature -> the EFFECTIVE relative tolerance the parity comparison should use (the type policy's
    rtol, or the feature's own override). Passed to ``compare_groups`` so a windowed feature's legitimate
    float-order noise isn't counted as a mismatch at the engine-default 1e-6."""
    return {feature: pol.rtol for feature, (_version, pol) in feature_policy_map().items()}


def content_hash_map() -> dict[str, str]:
    """feature -> content hash of its group's compute source (one hash per group, fanned to its features)."""
    hash_of_group = {group.name: group_content_hash(group) for group in REGISTRY.groups()}
    return {spec.name: hash_of_group[group.name] for group, spec in REGISTRY.feature_specs()}


def deterministic_features() -> list[str]:
    """Features TRUSTED by construction — CALENDAR (pure functions of the timestamp). Their parity is
    guaranteed, so they earn trust with no parity day."""
    return [
        spec.name
        for group, spec in REGISTRY.feature_specs()
        if group.type == FeatureType.CALENDAR
    ]


def earned_features(clean_today: pl.DataFrame, policy_of: dict[str, tuple[str, TrustTolerance]]) -> list[str]:
    """Features that EARNED trust on today's clean day: clean_value_rate >= their policy min_pass_rate.

    ``clean_today`` is the per-(feature, day) CLEAN rollup (feature, clean_value_rate, ...). The cell
    comparison already used each feature's effective tolerance, so clean_value_rate is the fraction of
    clean cells that agreed within tolerance; we threshold it at the per-type min_pass_rate."""
    if clean_today.height == 0:
        return []
    earned: list[str] = []
    for row in clean_today.to_dicts():
        feature = row["feature"]
        rate = row["clean_value_rate"]
        entry = policy_of.get(feature)
        if entry is None or rate is None:
            continue
        _version, pol = entry
        if not pol.deterministic and rate >= pol.min_pass_rate:
            earned.append(feature)
    return earned


_UPSERT_TRUST_GRANT = """
INSERT INTO feature_trust
  (feature, version, status, value_grade, coverage_grade, trust_state, trust_reason, trusted_at,
   trusted_day, trusted_git_commit, trusted_content_hash, trust_value_rate, trust_tolerance,
   trust_min_pass_rate)
VALUES (%(feature)s, %(version)s, 'trusted_binary', 'U', 'U', 'TRUSTED', %(reason)s, now(),
        %(day)s, %(git_commit)s, %(content_hash)s, %(value_rate)s, %(tolerance)s, %(min_pass_rate)s)
ON CONFLICT (feature, version) DO UPDATE SET
  trust_state='TRUSTED', trust_reason=EXCLUDED.trust_reason, trusted_at=now(),
  trusted_day=EXCLUDED.trusted_day, trusted_git_commit=EXCLUDED.trusted_git_commit,
  trusted_content_hash=EXCLUDED.trusted_content_hash, trust_value_rate=EXCLUDED.trust_value_rate,
  trust_tolerance=EXCLUDED.trust_tolerance, trust_min_pass_rate=EXCLUDED.trust_min_pass_rate,
  updated_at=now()
WHERE feature_trust.trust_state = 'NON_TRUSTED'
"""

_INSERT_CHECK = """
INSERT INTO feature_trust_check
  (feature, version, check_kind, checked_day, content_hash, git_commit, value_rate, tolerance,
   min_pass_rate, n_compared, passed, action)
VALUES (%(feature)s, %(version)s, %(check_kind)s, %(day)s, %(content_hash)s, %(git_commit)s,
        %(value_rate)s, %(tolerance)s, %(min_pass_rate)s, %(n_compared)s, true, %(action)s)
"""


def already_trusted(features: list[str]) -> set[str]:
    """The subset of ``features`` already TRUSTED — skipped so a grant never re-stamps (permanence) and the
    check history doesn't accumulate a redundant row every sweep."""
    if not features:
        return set()
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT feature FROM feature_trust WHERE trust_state='TRUSTED' AND feature = ANY(%s)",
            (features,),
        )
        return {row[0] for row in cur.fetchall()}


def _grant_params(
    feature: str,
    reason: str,
    day: str | None,
    value_rate: float | None,
    policy_of: dict[str, tuple[str, TrustTolerance]],
    content_hash_of: dict[str, str],
    git_commit: str | None,
) -> dict[str, object]:
    version, pol = policy_of[feature]
    return {
        "feature": feature,
        "version": version,
        "reason": reason,
        "day": day,
        "git_commit": git_commit,
        "content_hash": content_hash_of.get(feature),
        "value_rate": value_rate,
        "tolerance": pol.rtol,
        "min_pass_rate": None if pol.deterministic else pol.min_pass_rate,
    }


def write_trust_grants(
    earned: list[str],
    deterministic: list[str],
    clean_today: pl.DataFrame,
    day: str,
) -> dict[str, int]:
    """Promote newly-earned + deterministic features to TRUSTED (permanence: only NON_TRUSTED rows move),
    recording provenance and appending a feature_trust_check row per grant. Idempotent — re-running a day
    skips features already trusted and re-affirms nothing."""
    policy_of = feature_policy_map()
    content_hash_of = content_hash_map()
    git_commit = current_git_commit()
    rate_of = (
        {row["feature"]: row["clean_value_rate"] for row in clean_today.to_dicts()}
        if clean_today.height
        else {}
    )

    fresh_det = [f for f in deterministic if f not in set(already_trusted(deterministic))]
    fresh_earned = [f for f in earned if f not in set(already_trusted(earned)) and f not in set(fresh_det)]

    grant_rows: list[dict[str, object]] = []
    check_rows: list[dict[str, object]] = []
    for feature in fresh_det:
        params = _grant_params(feature, "deterministic", None, None, policy_of, content_hash_of, git_commit)
        grant_rows.append(params)
        check_rows.append({**params, "check_kind": "deterministic", "n_compared": 0, "action": "trusted"})
    for feature in fresh_earned:
        rate = rate_of.get(feature)
        params = _grant_params(feature, "parity_1day", day, rate, policy_of, content_hash_of, git_commit)
        grant_rows.append(params)
        check_rows.append({**params, "check_kind": "initial", "n_compared": 0, "action": "trusted"})

    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if grant_rows:
            cur.executemany(_UPSERT_TRUST_GRANT, grant_rows)
        if check_rows:
            cur.executemany(_INSERT_CHECK, check_rows)
        conn.commit()

    return {"deterministic_trusted": len(fresh_det), "earned_trusted": len(fresh_earned)}
