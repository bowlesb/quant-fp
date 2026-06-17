"""The TRUSTED-FEATURES list — the consumable SELECTION the backfill + modelling agents gate on.

Ben's directive: backfill + model only the carefully-selected features that have EARNED trust (parity
held across >= 2 CLEAN regular-session days; see docs/TRUST_METADATA.md). This module is the thin,
queryable surface over that trust state (feature_trust.lifecycle_state, written by trust_lifecycle):

  - ``trusted_features()``    -> the rich rows (feature, version, clean_days, clean_value_rate, ...) the
                                 backfill agent joins against its raw-data-coverage manifest to decide what
                                 to backfill next ("trusted AND not-yet-backfilled").
  - ``trusted_names()``       -> just the names (re-exported from trust_lifecycle for one import site).
  - ``cohort_summary()``      -> per-lifecycle-state counts (how big is the trusted cohort, what's PENDING
                                 next), for the operator/coordinator poll.

CLI: ``python -m quantlib.features.trusted_list [--json | --names | --summary]`` — prints the current
trusted cohort (default: a table). The list GROWS as the nightly sweep promotes PENDING -> VALIDATED, so
the backfill agent re-queries it each cycle and backfills the newly-trusted features incrementally.
"""
from __future__ import annotations

import json
import sys

import psycopg

from quantlib.features.trust_lifecycle import STATE_VALIDATED, trusted_feature_names
from quantlib.features.validation_db import DB_KWARGS

_TRUSTED_QUERY = """
SELECT feature, version, clean_days, clean_days_passed, clean_value_rate,
       last_validated_day, lifecycle_updated_at
FROM feature_trust
WHERE lifecycle_state = %s
ORDER BY feature
"""

_SUMMARY_QUERY = """
SELECT COALESCE(lifecycle_state, 'UNGRADED') AS state, count(*) AS n
FROM feature_trust
GROUP BY COALESCE(lifecycle_state, 'UNGRADED')
ORDER BY n DESC
"""


def trusted_features() -> list[dict[str, object]]:
    """The trusted cohort as rich rows (the modeling SELECTION + its parity evidence)."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_TRUSTED_QUERY, (STATE_VALIDATED,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def trusted_names() -> list[str]:
    """Just the trusted feature names (sorted) — the minimal SELECTION for a consumer that only needs names."""
    return sorted(trusted_feature_names())


def cohort_summary() -> dict[str, int]:
    """Per-lifecycle-state feature counts — how big the trusted cohort is and what is PENDING next."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_SUMMARY_QUERY)
        return {state: count for state, count in cur.fetchall()}


def main() -> None:
    args = sys.argv[1:]
    if "--summary" in args:
        summary = cohort_summary()
        print(json.dumps(summary, indent=2))
        return
    if "--names" in args:
        for name in trusted_names():
            print(name)
        return
    rows = trusted_features()
    if "--json" in args:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("trusted_features: 0 (no feature has earned VALIDATED yet — needs >= 2 clean RTH days)")
        return
    print(f"trusted_features: {len(rows)}")
    for row in rows:
        print(
            f"  {row['feature']:<32} v{row['version']:<8} "
            f"clean_days={row['clean_days']} rate={row['clean_value_rate']:.5f} "
            f"last={row['last_validated_day']}"
        )


if __name__ == "__main__":
    main()
