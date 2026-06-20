"""Within-Day Parity Certifier — PHASE 2b: the cert-stamp + trust-grant write path.

Per docs/WITHIN_DAY_PARITY_CERTIFICATION.md §3. When the phase-1 compare shows a group stably matched on
the settled window (phase 3 enforces the stability counter), this:

  1. writes the within_day_parity_cert stamp (status='certified' | 'defected' | 'skipped_*' | 'fix_pending')
  2. for a 'certified' feature, GRANTS binary trust the SAME way the nightly sweep does — reusing the
     existing trust_binary grant SQL + provenance helpers, only with trust_reason='within_day_parity'
     (gate-read: within-day == nightly by construction, just earned earlier in the day).

It REUSES rather than reinvents: feature_policy_map / content_hash_map / current_git_commit / _grant_params
/ the _UPSERT_TRUST_GRANT + _INSERT_CHECK SQL from trust_binary. It does NOT modify the shared
write_trust_grants (the nightly path stays byte-identical); the within-day reason is isolated here so live
activation is a single deliberate wiring (phase 3), never an accident.

⭐ SAFETY: ``dry_run=True`` is the DEFAULT. Phase 2 BUILDS + TESTS the write path; it does not live-grant
trust. A dry run logs exactly what WOULD be written (the grant rows, the cert rows) and returns the plan
without opening a DB connection. The Lead enables live granting (dry_run=False) in phase 3, gated on the
RTH-dent confirm. Permanence + idempotency are inherited from the reused SQL (only NON_TRUSTED → TRUSTED;
ON CONFLICT updates in place).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import psycopg

from quantlib.features.trust_binary import (_grant_params, already_trusted,
                                            content_hash_map,
                                            feature_policy_map)
from quantlib.features.trust_policy import current_git_commit
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("within_day_trust")

WITHIN_DAY_REASON = "within_day_parity"

# Cert statuses (mirrors db/init/13_within_day_parity.sql).
STATUS_CERTIFIED = "certified"
STATUS_DEFECTED = "defected"
STATUS_FIX_PENDING = "fix_pending"
STATUS_SKIPPED_UNSETTLED = "skipped_unsettled"
STATUS_SKIPPED_CONTAMINATED = "skipped_contaminated"

_UPSERT_CERT = """
INSERT INTO within_day_parity_cert
  (feature, version, group_name, cert_day, status, stable_cycles, window_minutes, value_rate,
   n_clean_symbols, n_compared, tolerance, min_pass_rate, settle_lag_min, git_commit, content_hash, reason)
VALUES (%(feature)s, %(version)s, %(group_name)s, %(cert_day)s, %(status)s, %(stable_cycles)s,
        %(window_minutes)s, %(value_rate)s, %(n_clean_symbols)s, %(n_compared)s, %(tolerance)s,
        %(min_pass_rate)s, %(settle_lag_min)s, %(git_commit)s, %(content_hash)s, %(reason)s)
ON CONFLICT (feature, version, cert_day) DO UPDATE SET
  status=EXCLUDED.status, stable_cycles=EXCLUDED.stable_cycles, window_minutes=EXCLUDED.window_minutes,
  value_rate=EXCLUDED.value_rate, n_clean_symbols=EXCLUDED.n_clean_symbols, n_compared=EXCLUDED.n_compared,
  tolerance=EXCLUDED.tolerance, min_pass_rate=EXCLUDED.min_pass_rate, settle_lag_min=EXCLUDED.settle_lag_min,
  git_commit=EXCLUDED.git_commit, content_hash=EXCLUDED.content_hash, reason=EXCLUDED.reason,
  certified_at=now()
"""

# Reuse the EXACT trust grant + check SQL from trust_binary (single source of truth for the trust write).
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


@dataclass
class CertResult:
    feature: str
    version: str
    group_name: str
    cert_day: str
    status: str
    value_rate: float | None
    stable_cycles: int
    window_minutes: int
    n_clean_symbols: int
    n_compared: int
    settle_lag_min: float
    reason: str | None = None


def cert_param(result: CertResult, content_hash: str | None, git_commit: str | None) -> dict[str, object]:
    """Build the within_day_parity_cert UPSERT param dict from a CertResult + provenance + policy."""
    policy_of = feature_policy_map()
    version, pol = policy_of[result.feature]
    return {
        "feature": result.feature,
        "version": version,
        "group_name": result.group_name,
        "cert_day": result.cert_day,
        "status": result.status,
        "stable_cycles": result.stable_cycles,
        "window_minutes": result.window_minutes,
        "value_rate": result.value_rate,
        "n_clean_symbols": result.n_clean_symbols,
        "n_compared": result.n_compared,
        "tolerance": pol.rtol,
        "min_pass_rate": None if pol.deterministic else pol.min_pass_rate,
        "settle_lag_min": result.settle_lag_min,
        "git_commit": git_commit,
        "content_hash": content_hash,
        "reason": result.reason,
    }


def plan_writes(
    results: list[CertResult],
    trusted_already: set[str] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Build (cert_rows, grant_rows, check_rows) for a batch of per-feature cert results — PURE, no DB.

    A 'certified' feature that is NOT already trusted earns a grant row (reason='within_day_parity') +
    a feature_trust_check row (check_kind='within_day', action='trusted'). Other statuses write only the
    cert stamp. Permanence is inherited from the grant SQL (only NON_TRUSTED → TRUSTED); ``trusted_already``
    is an OPTIONAL pre-filter to avoid spamming the audit log with re-affirmations — pass it (from
    already_trusted()) for a live write, or omit it (None) to keep this function DB-free (dry-run/tests)."""
    content_hash_of = content_hash_map()
    git_commit = current_git_commit()
    policy_of = feature_policy_map()
    trusted_already = trusted_already or set()

    cert_rows: list[dict[str, object]] = []
    grant_rows: list[dict[str, object]] = []
    check_rows: list[dict[str, object]] = []

    for result in results:
        cert_rows.append(cert_param(result, content_hash_of.get(result.feature), git_commit))
        if result.status != STATUS_CERTIFIED:
            continue
        if result.feature in trusted_already:
            continue
        params = _grant_params(
            result.feature,
            WITHIN_DAY_REASON,
            result.cert_day,
            result.value_rate,
            policy_of,
            content_hash_of,
            git_commit,
        )
        grant_rows.append(params)
        check_rows.append(
            {**params, "check_kind": "within_day", "n_compared": result.n_compared, "action": "trusted"}
        )
    return cert_rows, grant_rows, check_rows


def write_certifications(results: list[CertResult], dry_run: bool = True) -> dict[str, int]:
    """Write the cert stamps + (for certified, not-yet-trusted features) the binary-trust grants.

    ⭐ dry_run=True (DEFAULT) opens NO DB connection — it logs the exact plan and returns the counts, so
    phase-2 build/test never live-grants. dry_run=False (phase-3, Lead-enabled) executes the UPSERTs in a
    single transaction. Idempotent + permanence-safe via the reused SQL."""
    if dry_run:
        cert_rows, grant_rows, check_rows = plan_writes(results, trusted_already=None)
        counts = {"cert_rows": len(cert_rows), "grants": len(grant_rows), "checks": len(check_rows)}
        logger.info(
            "DRY-RUN (no DB write): would UPSERT %d cert stamps, GRANT %d features trust "
            "(reason=%s), append %d trust_check rows. certified-to-grant: %s",
            counts["cert_rows"],
            counts["grants"],
            WITHIN_DAY_REASON,
            counts["checks"],
            [row["feature"] for row in grant_rows],
        )
        return counts

    certified = [r.feature for r in results if r.status == STATUS_CERTIFIED]
    cert_rows, grant_rows, check_rows = plan_writes(
        results, trusted_already=already_trusted(certified) if certified else set()
    )
    counts = {"cert_rows": len(cert_rows), "grants": len(grant_rows), "checks": len(check_rows)}
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if cert_rows:
            cur.executemany(_UPSERT_CERT, cert_rows)
        if grant_rows:
            cur.executemany(_UPSERT_TRUST_GRANT, grant_rows)
        if check_rows:
            cur.executemany(_INSERT_CHECK, check_rows)
        conn.commit()
    logger.info(
        "LIVE: UPSERT %d cert stamps, GRANTED %d features trust (reason=%s), %d check rows on %s",
        counts["cert_rows"],
        counts["grants"],
        WITHIN_DAY_REASON,
        counts["checks"],
        results[0].cert_day if results else "?",
    )
    return counts


def certify_result_from_summary(
    feature: str,
    group_name: str,
    cert_day: dt.date,
    value_rate: float | None,
    n_compared: int,
    n_clean_symbols: int,
    stable_cycles: int,
    window_minutes: int,
    settle_lag_min: float,
    min_pass_rate: float,
) -> CertResult:
    """Map a phase-1 per-feature compare result + the phase-3 stability counter to a CertResult.

    'certified' requires a value_rate at/above the feature's min_pass_rate AND a stable run (stable_cycles
    is enforced by the phase-3 loop; here it is carried through as evidence). Below the bar → 'defected'."""
    if value_rate is not None and value_rate >= min_pass_rate:
        status = STATUS_CERTIFIED
        reason = None
    else:
        status = STATUS_DEFECTED
        reason = f"value_rate={value_rate} < min_pass_rate={min_pass_rate}"
    return CertResult(
        feature=feature,
        version="",  # resolved from policy at write time
        group_name=group_name,
        cert_day=cert_day.isoformat(),
        status=status,
        value_rate=value_rate,
        stable_cycles=stable_cycles,
        window_minutes=window_minutes,
        n_clean_symbols=n_clean_symbols,
        n_compared=n_compared,
        settle_lag_min=settle_lag_min,
        reason=reason,
    )
