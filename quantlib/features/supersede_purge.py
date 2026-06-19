"""On-trust SUPERSEDE-PURGE: when a (group, version) earns trust, reclaim the OLDER untrusted versions.

Ben's directive: once a feature's stream matches backfill and it becomes TRUSTED at a version, any PRIOR
version of that same feature still sitting in the store is stale, untrusted data we no longer want. This
module is the mechanism: given the trust grants from a sweep, it finds — per affected group — the on-disk
versions that are STRICTLY OLDER than a now-trusted version and themselves carry NO trusted feature, and
purges them via the existing reversible/registry-gated ``lifecycle.delete_feature_group``.

Trust is keyed ``(feature, version)`` in ``feature_trust``; the store is partitioned ``group=<G>/v=<V>/``.
A group is "trusted at version V" when ANY of its registry features is TRUSTED at V. A version is purgeable
for group G only when ALL of these hold (the REFUSE-GUARDS):

  1. it is STRICTLY LOWER than some trusted version of G (semantic version compare) — there IS a newer,
     trusted successor, so the old data is genuinely superseded;
  2. it is NOT the newest version of G present on disk — never purge the leading edge;
  3. NO feature of G is itself TRUSTED at that version — never purge data behind a trusted feature.

SAFETY: ``delete_feature_group`` refuses ``source=stream`` partitions unless ``include_stream=True`` (live
stream is irreplaceable — Alpaca only serves settled backfill). This planner SURFACES the stream date count
per candidate so the operator sees the permanent-loss cost; ``include_stream`` defaults False so a purge
reclaims only the reproducible backfill cache unless explicitly told otherwise.

Idempotent: re-running after a purge finds the version gone and plans nothing. Default DRY-RUN; an apply
requires the explicit ``apply=True`` flag.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import psycopg
from packaging.version import InvalidVersion, Version

from quantlib.features import lifecycle
from quantlib.features.registry import REGISTRY
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("supersede_purge")

DEFAULT_ROOT = "/store"

_KNOWN_STALE = [("momentum_run", "1.0.0"), ("price_levels", "1.0.0")]

_TRUST_BY_FEATURE = """
SELECT feature, version, trust_state
FROM feature_trust
WHERE feature = ANY(%(features)s)
"""


def _group_features() -> dict[str, list[str]]:
    """group name -> its registry feature names (the current code's declared features)."""
    features_of: dict[str, list[str]] = {}
    for group, spec in REGISTRY.feature_specs():
        features_of.setdefault(group.name, []).append(spec.name)
    return features_of


def _trusted_versions_of_group(group: str, features: list[str]) -> set[str]:
    """The set of versions at which AT LEAST ONE of ``group``'s features is TRUSTED. Empty => the group
    has earned no trust at any version, so nothing is superseded and nothing is purgeable."""
    if not features:
        return set()
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_TRUST_BY_FEATURE, {"features": features})
        return {
            version
            for feature, version, trust_state in cur.fetchall()
            if trust_state == "TRUSTED" and version is not None
        }


def _as_version(raw: str) -> Version | None:
    try:
        return Version(raw)
    except InvalidVersion:
        logger.warning("supersede_purge: unparseable version %r — treated as non-comparable, skipped", raw)
        return None


def purge_candidates(
    group: str, on_disk: list[dict[str, int | str]], trusted_versions: set[str]
) -> list[dict[str, object]]:
    """The pure guard logic (no DB / no filesystem): given a group's on-disk versions and the set of its
    TRUSTED versions, return the purgeable candidates. A version is purgeable iff it is STRICTLY OLDER than
    the newest trusted version (1), is NOT the newest version on disk (2), and is NOT itself trusted (3)."""
    if not on_disk:
        return []
    trusted_parsed = [parsed for version in trusted_versions if (parsed := _as_version(version)) is not None]
    if not trusted_parsed:
        return []
    max_trusted = max(trusted_parsed)

    disk_parsed = {str(row["version"]): _as_version(str(row["version"])) for row in on_disk}
    comparable = [parsed for parsed in disk_parsed.values() if parsed is not None]
    newest_on_disk = max(comparable) if comparable else None

    candidates: list[dict[str, object]] = []
    for row in on_disk:
        version = str(row["version"])
        parsed = disk_parsed[version]
        if parsed is None:
            continue
        is_strictly_older = parsed < max_trusted
        is_newest_on_disk = newest_on_disk is not None and parsed == newest_on_disk
        is_itself_trusted = version in trusted_versions
        if is_strictly_older and not is_newest_on_disk and not is_itself_trusted:
            candidates.append(
                {
                    "group": group,
                    "version": version,
                    "backfill_dates": int(row["backfill_dates"]),
                    "stream_dates": int(row["stream_dates"]),
                    "mb": round(int(row["bytes"]) / 1e6, 2),
                    "superseded_by_trusted": str(max_trusted),
                }
            )
    return candidates


def plan_group_purge_pure(
    root: str | Path, group: str, trusted_versions: set[str]
) -> list[dict[str, object]]:
    """The purge candidates for ONE group, reading the live STORE but taking the group's TRUSTED versions
    as an explicit argument (no DB). The filesystem half of the planner — used by tests/tooling that supply
    trust directly."""
    on_disk = lifecycle.store_versions(root, group)
    return purge_candidates(group, on_disk, trusted_versions)


def plan_group_purge(root: str | Path, group: str, features: list[str]) -> list[dict[str, object]]:
    """The purge candidates for ONE group, reading the live store + trust DB. Thin wrapper over the pure
    ``purge_candidates`` guard logic. Each row carries the backfill/stream date counts + MB so the operator
    sees exactly what (and how irreplaceable) the candidate is."""
    trusted_versions = _trusted_versions_of_group(group, features)
    return plan_group_purge_pure(root, group, trusted_versions)


def plan_purge(root: str | Path, groups: list[str] | None = None) -> list[dict[str, object]]:
    """The full purge plan across ``groups`` (or every registered group). Pure read: no deletion."""
    features_of = _group_features()
    target_groups = groups if groups is not None else sorted(features_of)
    plan: list[dict[str, object]] = []
    for group in target_groups:
        plan.extend(plan_group_purge(root, group, features_of.get(group, [])))
    return plan


def execute_purge(
    root: str | Path,
    candidates: list[dict[str, object]],
    apply: bool,
    include_stream: bool = False,
) -> list[dict[str, object]]:
    """Apply (or dry-run) the plan. For each candidate calls ``delete_feature_group(root, group, version)``
    — which itself REFUSES stream partitions unless ``include_stream=True``. Returns one result row per
    candidate. With ``apply=False`` (default) nothing is deleted; the rows report what WOULD be freed."""
    results: list[dict[str, object]] = []
    for candidate in candidates:
        group = str(candidate["group"])
        version = str(candidate["version"])
        if not apply:
            logger.info(
                "DRY-RUN supersede-purge: group=%s v=%s backfill_dates=%s stream_dates=%s mb=%s "
                "(superseded by trusted v%s) — would delete; pass --apply to execute",
                group,
                version,
                candidate["backfill_dates"],
                candidate["stream_dates"],
                candidate["mb"],
                candidate["superseded_by_trusted"],
            )
            results.append({**candidate, "applied": False, "deleted": "dry_run"})
            continue
        entry = lifecycle.delete_feature_group(root, group, version, include_stream=include_stream)
        logger.info(
            "APPLIED supersede-purge: group=%s v=%s partitions=%s mb_freed=%s",
            group,
            version,
            entry["partitions"],
            entry["mb_freed"],
        )
        results.append({**candidate, "applied": True, "deleted": entry})
    return results


def supersede_purge_for_grants(
    root: str | Path,
    newly_trusted_features: list[str],
    apply: bool = False,
    include_stream: bool = False,
) -> list[dict[str, object]]:
    """The WIRE-POINT hook: given the features that just earned trust in a sweep, purge the older untrusted
    versions of ONLY the groups those features belong to. Default DRY-RUN. Safe to call every sweep — it is
    idempotent and a no-op when no grant supersedes an older on-disk version."""
    if not newly_trusted_features:
        return []
    features_of = _group_features()
    group_of_feature = {feature: group for group, features in features_of.items() for feature in features}
    affected_groups = sorted(
        {group_of_feature[feature] for feature in newly_trusted_features if feature in group_of_feature}
    )
    if not affected_groups:
        return []
    candidates = plan_purge(root, affected_groups)
    return execute_purge(root, candidates, apply=apply, include_stream=include_stream)


def _parse_args(args: list[str]) -> dict[str, object]:
    apply = "--apply" in args
    include_stream = "--include-stream" in args
    known_stale = "--known-stale" in args
    as_json = "--json" in args
    rest = [arg for arg in args if arg not in ("--apply", "--include-stream", "--known-stale", "--json")]
    root = DEFAULT_ROOT
    groups: list[str] | None = None
    iterator = iter(rest)
    for arg in iterator:
        if arg == "--root":
            root = next(iterator)
        elif arg == "--group":
            groups = (groups or []) + [next(iterator)]
    return {
        "root": root,
        "groups": groups,
        "apply": apply,
        "include_stream": include_stream,
        "known_stale": known_stale,
        "as_json": as_json,
    }


def _known_stale_candidates(root: str | Path) -> list[dict[str, object]]:
    """The plan restricted to the two operator-known stale versions (momentum_run/price_levels v1.0.0),
    cross-checked against the live trust + on-disk state (a version that turns out trusted or newest is
    dropped by the guards in ``plan_group_purge``)."""
    features_of = _group_features()
    candidates: list[dict[str, object]] = []
    for group, version in _KNOWN_STALE:
        for candidate in plan_group_purge(root, group, features_of.get(group, [])):
            if candidate["version"] == version:
                candidates.append(candidate)
    return candidates


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    options = _parse_args(sys.argv[1:])
    root = str(options["root"])
    groups = options["groups"]
    apply = bool(options["apply"])
    include_stream = bool(options["include_stream"])

    if options["known_stale"]:
        candidates = _known_stale_candidates(root)
    else:
        candidates = plan_purge(root, groups if isinstance(groups, list) else None)

    results = execute_purge(root, candidates, apply=apply, include_stream=include_stream)

    if options["as_json"]:
        print(json.dumps(results, indent=2, default=str))
        return

    mode = "APPLY" if apply else "DRY-RUN"
    stream_note = "" if include_stream else " (backfill only; stream refused — pass --include-stream)"
    print(f"supersede-purge [{mode}]{stream_note}: {len(results)} candidate(s)")
    for result in results:
        print(
            f"  group={result['group']:<16} v={result['version']:<8} "
            f"backfill_dates={result['backfill_dates']} stream_dates={result['stream_dates']} "
            f"mb={result['mb']} superseded_by=v{result['superseded_by_trusted']} "
            f"applied={result['applied']}"
        )
    if not results:
        print("  (nothing to purge — no older untrusted version is superseded by a trusted one)")


if __name__ == "__main__":
    main()
