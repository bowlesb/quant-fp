"""Unit tests for the CI scope classifier — the AUTO vs GATED safety boundary (ops/ci_scope).

These are the safety-critical predicates: a wrong TIER-1 verdict could auto-deploy a fingerprint change into
the live trading loop. Named test_fp_* so the CI gate runs them on itself.
"""

from __future__ import annotations

from ops.ci_scope import (
    Tier,
    classify,
    deploy_target,
    path_is_danger,
    path_is_safe,
)

FP = 0x873F2FCEB8F00C92  # an arbitrary stable fingerprint value


def test_dashboard_only_fp_neutral_is_auto() -> None:
    result = classify(
        ["services/dashboard/app.py", "frontend/src/Latency.jsx"],
        fingerprint_base=FP,
        fingerprint_head=FP,
    )
    assert result.tier is Tier.AUTO
    assert result.is_auto
    assert not result.danger_paths
    assert not result.unrecognized_paths


def test_docs_only_is_auto() -> None:
    result = classify(["docs/CONTINUOUS_DEPLOY.md", "README.md"], FP, FP)
    assert result.tier is Tier.AUTO


def test_ops_and_tests_only_is_auto() -> None:
    result = classify(["ops/ci_watcher.py", "tests/test_fp_ci_scope.py"], FP, FP)
    assert result.tier is Tier.AUTO


def test_fingerprint_moved_forces_gated() -> None:
    # Even if every path looked safe, a moved fingerprint must gate (fc/strategies recompile).
    result = classify(["docs/foo.md"], fingerprint_base=FP, fingerprint_head=FP + 1)
    assert result.tier is Tier.GATED
    assert not result.fingerprint_unchanged
    assert any("fingerprint moved" in reason for reason in result.reasons)


def test_feature_group_change_is_gated() -> None:
    result = classify(["quantlib/features/groups/momentum.py"], FP, FP)
    assert result.tier is Tier.GATED
    assert "quantlib/features/groups/momentum.py" in result.danger_paths


def test_fc_change_is_gated() -> None:
    result = classify(["services/fc/main.py"], FP, FP)
    assert result.tier is Tier.GATED
    assert result.danger_paths


def test_strategy_change_is_gated() -> None:
    result = classify(["services/strategies/reversion/strat.py"], FP, FP)
    assert result.tier is Tier.GATED


def test_rust_kernel_change_is_gated() -> None:
    result = classify(["rust/src/tick.rs"], FP, FP)
    assert result.tier is Tier.GATED


def test_mixed_safe_and_danger_is_gated() -> None:
    # One danger path poisons the whole PR (fail-closed) — a doc tweak can't smuggle an fc change.
    result = classify(
        ["docs/foo.md", "services/fc/loop.py"],
        FP,
        FP,
    )
    assert result.tier is Tier.GATED
    assert "services/fc/loop.py" in result.danger_paths


def test_unrecognized_path_is_gated() -> None:
    # A path on neither list is unknown scope → fail-closed to TIER-2.
    result = classify(["some/random/newdir/thing.py"], FP, FP)
    assert result.tier is Tier.GATED
    assert "some/random/newdir/thing.py" in result.unrecognized_paths


def test_empty_diff_is_gated() -> None:
    result = classify([], FP, FP)
    assert result.tier is Tier.GATED
    assert any("empty diff" in reason for reason in result.reasons)


def test_path_is_danger_predicates() -> None:
    assert path_is_danger("services/fc/x.py")
    assert path_is_danger("quantlib/features/groups/x.py")
    assert path_is_danger("quantlib/bus/schema.py")
    assert path_is_danger("rust/lib.rs")
    assert not path_is_danger("services/dashboard/x.py")
    assert not path_is_danger("docs/x.md")


def test_path_is_safe_predicates() -> None:
    assert path_is_safe("services/dashboard/x.py")
    assert path_is_safe("docs/x.md")
    assert path_is_safe("tests/test_fp_x.py")
    assert path_is_safe("ops/x.py")
    assert path_is_safe("README.md")
    assert not path_is_safe("services/fc/x.py")
    assert not path_is_safe("random/thing.py")


def test_deploy_target_dashboard() -> None:
    assert deploy_target(["services/dashboard/app.py"]) == "dashboard"
    assert deploy_target(["frontend/src/x.jsx"]) == "dashboard"
    assert deploy_target(["services/dashboard/app.py", "frontend/src/x.jsx"]) == "dashboard"


def test_deploy_target_docs_only_is_none() -> None:
    # Doc/test/ops-only PRs have nothing to restart — they just merge.
    assert deploy_target(["docs/x.md", "tests/test_fp_x.py"]) is None


def test_deploy_target_multi_service_is_none() -> None:
    # Spanning two services → escalate (a single auto-deploy restarts ONE container).
    assert deploy_target(["services/dashboard/x.py", "services/other/y.py"]) is None
