"""Write crypto trust grants into the crypto-namespaced ledger (db/init/15_crypto_trust.sql).

The crypto rehearsal reuses the EQUITY grading logic unchanged (``trust_binary.earned_features``,
``trust_lifecycle.clean_feature_day``, the feature policy map) but writes the grant to ``crypto_feature_trust``
keyed by ``asset_class`` instead of the equity ``feature_trust`` — so a crypto grant is PHYSICALLY separate
from the equity grant for the same feature name (docs/CRYPTO_E2E.md §1, the separation principle). Mirrors
``trust_binary.write_trust_grants`` (same provenance + append-only check history), retargeted at the crypto
ledger; only NON_TRUSTED rows move (permanence), idempotent across re-runs.
"""
from __future__ import annotations

import polars as pl
import psycopg

from quantlib.features.trust_binary import (
    content_hash_map,
    feature_policy_map,
)
from quantlib.features.trust_policy import current_git_commit
from quantlib.features.validation_db import DB_KWARGS

ASSET_CLASS = "crypto"

_UPSERT_CRYPTO_GRANT = """
INSERT INTO crypto_feature_trust
  (asset_class, feature, version, trust_state, trust_reason, trusted_at, trusted_day,
   trusted_git_commit, trusted_content_hash, trust_value_rate, trust_tolerance, trust_min_pass_rate)
VALUES (%(asset_class)s, %(feature)s, %(version)s, 'TRUSTED', %(reason)s, now(), %(day)s,
        %(git_commit)s, %(content_hash)s, %(value_rate)s, %(tolerance)s, %(min_pass_rate)s)
ON CONFLICT (asset_class, feature, version) DO UPDATE SET
  trust_state='TRUSTED', trust_reason=EXCLUDED.trust_reason, trusted_at=now(),
  trusted_day=EXCLUDED.trusted_day, trusted_git_commit=EXCLUDED.trusted_git_commit,
  trusted_content_hash=EXCLUDED.trusted_content_hash, trust_value_rate=EXCLUDED.trust_value_rate,
  trust_tolerance=EXCLUDED.trust_tolerance, trust_min_pass_rate=EXCLUDED.trust_min_pass_rate,
  updated_at=now()
WHERE crypto_feature_trust.trust_state = 'NON_TRUSTED'
"""

_INSERT_CRYPTO_CHECK = """
INSERT INTO crypto_trust_check
  (asset_class, feature, version, check_kind, checked_day, content_hash, git_commit, value_rate,
   tolerance, min_pass_rate, n_compared, passed, action)
VALUES (%(asset_class)s, %(feature)s, %(version)s, %(check_kind)s, %(day)s, %(content_hash)s,
        %(git_commit)s, %(value_rate)s, %(tolerance)s, %(min_pass_rate)s, %(n_compared)s, true, %(action)s)
"""


def already_trusted(features: list[str]) -> set[str]:
    """The subset of ``features`` already TRUSTED in the CRYPTO ledger — skipped so a grant never re-stamps."""
    if not features:
        return set()
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT feature FROM crypto_feature_trust "
            "WHERE asset_class=%s AND trust_state='TRUSTED' AND feature = ANY(%s)",
            (ASSET_CLASS, features),
        )
        return {row[0] for row in cur.fetchall()}


def write_crypto_grants(earned: list[str], clean_today: pl.DataFrame, day: str) -> dict[str, int]:
    """Promote newly-earned crypto features to TRUSTED in ``crypto_feature_trust`` (only NON_TRUSTED rows
    move), with provenance + an append-only ``crypto_trust_check`` row per grant. Idempotent: re-running a
    day skips already-trusted crypto features. Equity ``feature_trust`` is NEVER touched."""
    policy_of = feature_policy_map()
    content_hash_of = content_hash_map()
    git_commit = current_git_commit()
    rate_of = (
        {row["feature"]: row["clean_value_rate"] for row in clean_today.to_dicts()}
        if clean_today.height
        else {}
    )

    fresh = [f for f in earned if f not in already_trusted(earned) and f in policy_of]
    grant_rows: list[dict[str, object]] = []
    check_rows: list[dict[str, object]] = []
    for feature in fresh:
        version, pol = policy_of[feature]
        params = {
            "asset_class": ASSET_CLASS,
            "feature": feature,
            "version": version,
            "reason": "parity_1day",
            "day": day,
            "git_commit": git_commit,
            "content_hash": content_hash_of.get(feature),
            "value_rate": rate_of.get(feature),
            "tolerance": pol.rtol,
            "min_pass_rate": pol.min_pass_rate,
        }
        grant_rows.append(params)
        check_rows.append({**params, "check_kind": "initial", "n_compared": 0, "action": "trusted"})

    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if grant_rows:
            cur.executemany(_UPSERT_CRYPTO_GRANT, grant_rows)
        if check_rows:
            cur.executemany(_INSERT_CRYPTO_CHECK, check_rows)
        conn.commit()

    return {"earned_trusted": len(fresh)}
