"""Random re-checks of TRUSTED features (docs/TRUST_REDESIGN.md) — the safety net that makes 1-day trust
safe.

Trust is earned on a single clean day. To catch the rare feature that passed by luck, or that a later
code/data change silently broke, a scheduled job periodically re-checks TRUSTED features against fresh
backfill on a random recent CLEAN day. A clean-day failure is the ONLY thing that un-trusts a feature
(trust is otherwise permanent) — a deliberate, logged, contamination-aware action.

Mechanism (maximal reuse, minimal new risk): pick a random recent day we have already graded (so its raw
tape is present and it cleared the cleanliness gate), re-run the SAME sweep that earns trust (idempotent),
then re-verify every currently-TRUSTED feature against its per-type threshold on that day. A feature that
graded BELOW its threshold on this clean day is un-trusted, a defect is filed, and a human/agent decides:
the check was unsound (keep) or the divergence is real (fix + re-earn at a new version, or deprecate).

A graded day's illiquid raw tail can fall back below the settle gate by the time the random check re-runs,
so ``sweep_day`` re-raises ``RawNotSettledError`` — a benign transient the nightly sweep already SKIPs. The
random check skips such a day and tries the next one in the (shuffled) pool; if none is currently settled it
exits cleanly with a note and retries next run. It never crashes the weekly cron on a transient.

Deterministic features are not re-checked (their parity is guaranteed by construction).
"""

from __future__ import annotations

import json
import random
import sys
from functools import partial
from typing import Callable

import psycopg

from quantlib.features.materialize import DEFAULT_RAW_ROOT
from quantlib.features.registry import REGISTRY
from quantlib.features.trust_binary import feature_policy_map
from quantlib.features.validation_db import DB_KWARGS
from quantlib.features.validation_sweep import RawNotSettledError, sweep_day

DEFAULT_FEATURE_ROOT = "/store"
DEFAULT_VAL_ROOT = "/store/_validation"
RECENT_DAYS_POOL = 30  # sample the random check-day from the most recent N graded days


def failed_check(value_rate: float | None, min_pass_rate: float | None) -> bool:
    """A TRUSTED feature FAILS a re-check iff it was compared on the day AND its match rate fell below the
    threshold that earned it. No comparison (value_rate None) is NOT a failure — absence of evidence on one
    random day never un-trusts (we only act on a positive clean-day disagreement)."""
    if value_rate is None or min_pass_rate is None:
        return False
    return value_rate < min_pass_rate


def recent_graded_days(limit: int = RECENT_DAYS_POOL) -> list[str]:
    """The most recent days a sweep has graded — each has raw present and passed the cleanliness gate, so it
    is a fair re-check day. The random check samples its day from this pool."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT day FROM feature_validation_day ORDER BY day DESC LIMIT %s", (limit,))
        return [str(row[0]) for row in cur.fetchall()]


def graded_value_rates(day: str) -> dict[str, tuple[float | None, int]]:
    """feature -> (value_rate, n_compared) from the day's all-tiers rollup (tier 0). The re-check reads this
    after re-running the sweep for ``day``."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT feature, value_rate, n_compared FROM feature_validation_day WHERE day=%s AND tier=0",
            (day,),
        )
        return {feature: (value_rate, n_compared) for feature, value_rate, n_compared in cur.fetchall()}


def trusted_checkable() -> dict[str, str]:
    """TRUSTED, non-deterministic features -> their version. These are the features a random check
    re-verifies (deterministic features are guaranteed and skipped)."""
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT feature, version FROM feature_trust "
            "WHERE trust_state='TRUSTED' AND COALESCE(trust_reason,'') <> 'deterministic'"
        )
        return {feature: version for feature, version in cur.fetchall()}


_INSERT_CHECK = """
INSERT INTO feature_trust_check
  (feature, version, check_kind, checked_day, value_rate, min_pass_rate, n_compared, passed, action)
VALUES (%(feature)s, %(version)s, 'random', %(day)s, %(value_rate)s, %(min_pass_rate)s, %(n_compared)s,
        %(passed)s, %(action)s)
"""

_UNTRUST = """
UPDATE feature_trust
   SET trust_state='NON_TRUSTED', trust_reason='random_check_failed', untrusted_at=now(), updated_at=now()
 WHERE feature=%(feature)s AND version=%(version)s AND trust_state='TRUSTED'
"""

_FILE_DEFECT = """
INSERT INTO feature_parity_defect
  (feature, version, feature_group, status, first_seen_day, last_seen_day, clean_days_failed, worst_rel_err, exemplars)
VALUES (%(feature)s, %(version)s, %(feature_group)s, 'open', %(day)s, %(day)s, 1, NULL, '[]')
ON CONFLICT (feature, version) DO UPDATE SET
  last_seen_day=GREATEST(feature_parity_defect.last_seen_day, EXCLUDED.last_seen_day),
  status=CASE WHEN feature_parity_defect.status IN ('fixed','wontfix','auto_closed') THEN 'open'
              ELSE feature_parity_defect.status END,
  clean_streak=0,
  last_streak_day=NULL,
  updated_at=now()
"""


def apply_recheck(day: str, group_of: dict[str, str]) -> dict[str, object]:
    """Re-verify every TRUSTED non-deterministic feature against its threshold on ``day`` (already swept).
    Un-trust + file a defect for clean-day failures; append a check row for every feature compared."""
    checkable = trusted_checkable()
    policy_of = feature_policy_map()
    rates = graded_value_rates(day)

    untrusted: list[str] = []
    reaffirmed = 0
    check_rows: list[dict[str, object]] = []
    untrust_rows: list[dict[str, object]] = []
    defect_rows: list[dict[str, object]] = []

    for feature, version in checkable.items():
        entry = policy_of.get(feature)
        if entry is None:
            continue
        _version, pol = entry
        value_rate, n_compared = rates.get(feature, (None, 0))
        if value_rate is None:
            continue  # not compared on this day — no evidence, no action
        passed = not failed_check(value_rate, pol.min_pass_rate)
        action = "reaffirmed" if passed else "untrusted"
        check_rows.append(
            {
                "feature": feature,
                "version": version,
                "day": day,
                "value_rate": value_rate,
                "min_pass_rate": pol.min_pass_rate,
                "n_compared": n_compared,
                "passed": passed,
                "action": action,
            }
        )
        if passed:
            reaffirmed += 1
        else:
            untrusted.append(feature)
            untrust_rows.append({"feature": feature, "version": version})
            defect_rows.append(
                {
                    "feature": feature,
                    "version": version,
                    "feature_group": group_of.get(feature, "?"),
                    "day": day,
                }
            )

    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        if untrust_rows:
            cur.executemany(_UNTRUST, untrust_rows)
        if defect_rows:
            cur.executemany(_FILE_DEFECT, defect_rows)
        if check_rows:
            cur.executemany(_INSERT_CHECK, check_rows)
        conn.commit()

    return {
        "day": day,
        "checked": len(check_rows),
        "reaffirmed": reaffirmed,
        "untrusted": len(untrusted),
        "untrusted_features": untrusted[:20],
    }


def _sweep_one_day(feature_root: str, val_root: str, raw_root: str, day: str) -> None:
    """Re-run the idempotent sweep for one day (re-asserts the settle gate). ``day`` is last so a roots-bound
    ``functools.partial`` yields the ``Callable[[str], None]`` that ``sweep_first_settled`` drives."""
    sweep_day(feature_root=feature_root, val_root=val_root, day=day, raw_root=raw_root)


def sweep_first_settled(
    pool: list[str], sweep: Callable[[str], None], seed: int | None = None
) -> tuple[str | None, list[str]]:
    """Shuffle ``pool`` and re-sweep days until one SETTLES; return (the swept day, the skipped days).

    A re-check day must be re-sweepable RIGHT NOW. ``recent_graded_days`` returns days that graded once, but
    a day's illiquid raw tail can fall back below the settle gate (or its raw be pruned), so ``sweep_day``
    re-raises ``RawNotSettledError`` for it. That is a benign transient — the main sweep CLI already SKIPs it
    (exit 0) rather than failing — so the random check must skip it too and try the next day, not crash the
    weekly cron (the 2026-06-20 first-fire crash). Returns ``(None, skipped)`` if the whole pool is unsettled,
    so the caller can exit cleanly with a note. ``seed`` makes the order reproducible for tests/replay."""
    shuffled = list(pool)
    random.Random(seed).shuffle(shuffled)
    skipped: list[str] = []
    for day in shuffled:
        try:
            sweep(day)
        except RawNotSettledError:
            skipped.append(day)
            continue
        return day, skipped
    return None, skipped


def run_random_check(
    feature_root: str = DEFAULT_FEATURE_ROOT,
    val_root: str = DEFAULT_VAL_ROOT,
    raw_root: str = DEFAULT_RAW_ROOT,
    day: str | None = None,
    seed: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Pick a random recent clean day (unless ``day`` is given), re-run the sweep for it (idempotent), then
    re-verify the TRUSTED cohort. Days whose raw tail is not currently settled are skipped (the next pool day
    is tried) so the weekly cron never crashes on a transient. ``dry_run`` reports the trusted cohort that
    WOULD be re-checked WITHOUT sweeping or mutating any trust state. ``seed`` makes the day choice
    reproducible for tests/replay."""
    group_of = {spec.name: group.name for group, spec in REGISTRY.feature_specs()}

    if dry_run:
        pool = [day] if day is not None else recent_graded_days()
        checkable = trusted_checkable()
        return {
            "dry_run": True,
            "candidate_days": pool[:10],
            "n_candidate_days": len(pool),
            "trusted_checkable": len(checkable),
            "note": "would sweep the first settled candidate day and re-verify the trusted cohort; no mutation",
        }

    sweep = partial(_sweep_one_day, feature_root, val_root, raw_root)

    if day is not None:
        sweep(day)  # explicit day: let RawNotSettledError raise (operator asked for THIS day)
        return apply_recheck(day, group_of)

    pool = recent_graded_days()
    if not pool:
        return {"day": None, "checked": 0, "note": "no graded days to sample a random check from"}
    swept_day, skipped = sweep_first_settled(pool, sweep, seed=seed)
    if swept_day is None:
        return {
            "day": None,
            "checked": 0,
            "skipped_unsettled": len(skipped),
            "note": "no recent graded day is currently settled to re-sweep — skipping (will retry next run)",
        }
    result = apply_recheck(swept_day, group_of)
    result["skipped_unsettled"] = len(skipped)
    return result


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    day = next((arg for arg in args if len(arg) == 10 and arg[4] == "-"), None)
    result = run_random_check(day=day, dry_run=dry_run)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
