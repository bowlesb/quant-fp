"""WDPC continuous-deployment — the AUTO-MERGE-IN-SCOPE gate (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md
§4). Decides whether a subagent's fix may auto-deploy (real-time hot-swap) vs must escalate to the Lead.

A fix AUTO-DEPLOYS iff ALL conditions hold (fail-closed). Each is a pure, independently-testable predicate
composed here:
  1. OWNED-SCOPE ONLY   — the diff touches ONLY the assigned group's private file set.
  2. FINGERPRINT UNCHANGED — BusSchema.from_registry() fingerprint byte-identical before/after.
  3. PARITY FLIPS        — the fix turns the feature's verdict MISMATCH -> CLEAN (the reconfirm proof).
  4. BYTE-EQ ELSEWHERE   — every OTHER group byte-identical before/after on the same window.
  5. VALUE-CHANGE ONLY ON THE UNTRUSTED FEATURE — the assigned feature is NON_TRUSTED (not traded), and no
     trusted feature's values move.
  6. ADEQUATE UNIT TESTS + QA GREEN — the group's tests + parity suite pass (Ben's explicit deploy gate).
  7. HOT-SWAP-SAFE KIND  — the group is DIRECT-swappable or RESEED with a passing reseed (not ESCALATE).

Any failure -> ScopeViolation -> escalate to the Lead, never auto-merge. Composes ``ops/bus_compat_gate``
(fingerprint/contract safety) + the WDPC in-sandbox reconfirm. PURE: takes already-computed evidence; it
runs no git / DB / live state itself, so it is fully unit-testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateEvidence:
    """The already-gathered evidence the gate decides over (pure inputs — the caller collects these)."""

    group_name: str
    owned_feature: str
    changed_files: list[str]  # `git diff --name-only` of the fix
    owned_file_set: list[str]  # the group's declared private file set
    fingerprint_before: int
    fingerprint_after: int
    parity_was_mismatch: bool  # the feature was failing before the fix
    parity_now_clean: bool  # the reconfirm proof: compute_latest == compute after the fix
    differing_other_groups: list[str]  # byte_eq_elsewhere result (empty = surgical)
    owned_feature_is_untrusted: bool  # the assigned feature is NON_TRUSTED (not traded)
    trusted_features_moved: list[str]  # trusted features whose values changed (must be empty)
    unit_tests_passed: bool
    qa_clean: bool
    swap_kind: str  # 'direct' | 'reseed' | 'escalate'


@dataclass
class GateResult:
    approved: bool
    violations: list[str] = field(default_factory=list)


def evaluate(evidence: GateEvidence) -> GateResult:
    """Approve the fix for auto-deploy iff ALL §4 conditions hold; else list every violation (fail-closed)."""
    violations: list[str] = []

    out_of_scope = [path for path in evidence.changed_files if path not in set(evidence.owned_file_set)]
    if out_of_scope:
        violations.append(f"owned-scope: diff touches non-owned files {out_of_scope}")
    if not evidence.changed_files:
        violations.append("owned-scope: empty diff (nothing to deploy)")

    if evidence.fingerprint_before != evidence.fingerprint_after:
        violations.append(
            f"fingerprint changed {evidence.fingerprint_before:#018x} -> {evidence.fingerprint_after:#018x}"
        )

    if not (evidence.parity_was_mismatch and evidence.parity_now_clean):
        violations.append(
            f"parity gate did not flip mismatch->clean (was_mismatch={evidence.parity_was_mismatch}, "
            f"now_clean={evidence.parity_now_clean})"
        )

    if evidence.differing_other_groups:
        violations.append(f"not byte-eq elsewhere: {evidence.differing_other_groups} changed")

    if not evidence.owned_feature_is_untrusted:
        violations.append(f"owned feature '{evidence.owned_feature}' is TRUSTED — out of auto-deploy scope")
    if evidence.trusted_features_moved:
        violations.append(f"trusted feature values moved: {evidence.trusted_features_moved}")

    if not evidence.unit_tests_passed:
        violations.append("adequate unit tests did NOT pass")
    if not evidence.qa_clean:
        violations.append("QA not clean (ruff/black/isort/mypy)")

    if evidence.swap_kind == "escalate":
        violations.append(
            "hot-swap kind = ESCALATE (fingerprint-affecting / unseedable) — not auto-deployable"
        )
    elif evidence.swap_kind not in ("direct", "reseed"):
        violations.append(f"unknown swap kind '{evidence.swap_kind}'")

    return GateResult(approved=not violations, violations=violations)
