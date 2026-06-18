"""The TRUSTED-FEATURES list — the consumable SELECTION the backfill + modelling agents gate on.

Ben's directive: backfill + model only the features that have EARNED trust. Under the binary trust model
(docs/TRUST_REDESIGN.md) that is a single predicate — ``feature_trust.trust_state = 'TRUSTED'`` — for
features that are deterministic-by-construction or whose stream matched backfill within tolerance on a
clean day. This module is the thin, queryable surface over that state:

  - ``trusted_features()``    -> the rich rows (feature, version, trust_reason, trust_value_rate, the
                                 provenance) the backfill agent joins against its coverage manifest to decide
                                 what to backfill next ("trusted AND not-yet-backfilled").
  - ``trusted_names()``       -> just the names.
  - ``cohort_summary()``      -> TRUSTED vs NON_TRUSTED counts, for the operator/coordinator poll.

CLI: ``python -m quantlib.features.trusted_list [--json | --names | --summary]``. The list GROWS as the
nightly sweep earns trust (one clean day) and auto-trusts deterministic features, so the backfill agent
re-queries it each cycle.
"""
from __future__ import annotations

import json
import sys

import psycopg

from quantlib.features.validation_db import DB_KWARGS

TRUST_STATE_TRUSTED = "TRUSTED"

_TRUSTED_QUERY = """
SELECT feature, version, trust_reason, trusted_day, trust_value_rate, trust_tolerance,
       trusted_git_commit, trusted_at
FROM feature_trust
WHERE trust_state = 'TRUSTED'
ORDER BY feature
"""

_NAMES_QUERY = "SELECT feature FROM feature_trust WHERE trust_state = 'TRUSTED'"

_SUMMARY_QUERY = """
SELECT trust_state, count(*) AS n
FROM feature_trust
GROUP BY trust_state
ORDER BY n DESC
"""


def trusted_features() -> list[dict[str, object]]:
    """The trusted cohort as rich rows (the modeling SELECTION + its trust provenance)."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_TRUSTED_QUERY)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def trusted_feature_names() -> set[str]:
    """The set of TRUSTED feature names — the predicate downstream consumers (backfill, training export,
    strategies) intersect their requested features with. Empty until the first feature earns trust."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_NAMES_QUERY)
        return {row[0] for row in cur.fetchall()}


def trusted_names() -> list[str]:
    """Just the trusted feature names (sorted)."""
    return sorted(trusted_feature_names())


def cohort_summary() -> dict[str, int]:
    """TRUSTED vs NON_TRUSTED feature counts — how big the trusted cohort is."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_SUMMARY_QUERY)
        return {state: count for state, count in cur.fetchall()}


def main() -> None:
    args = sys.argv[1:]
    if "--summary" in args:
        print(json.dumps(cohort_summary(), indent=2))
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
        print("trusted_features: 0 (no feature TRUSTED yet — needs deterministic-by-construction or 1 clean day)")
        return
    print(f"trusted_features: {len(rows)}")
    for row in rows:
        rate = row["trust_value_rate"]
        rate_str = f"{rate:.5f}" if rate is not None else "  exact"
        print(
            f"  {row['feature']:<32} v{row['version']:<8} "
            f"{str(row['trust_reason']):<16} rate={rate_str} day={row['trusted_day']}"
        )


if __name__ == "__main__":
    main()
