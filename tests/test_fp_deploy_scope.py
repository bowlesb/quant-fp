"""Unit tests for the auto-deploy scope/path-map (ops.deploy_scope) — the safety core.

The critical properties: (1) fc/fingerprint-surface changes are ALWAYS TIER-2 (coordinated relaunch), never
hot-deployed; (2) self-contained services (dashboard/strategies/news) are TIER-1 and rebuild-by-name; (3) an
unknown container-bearing path ESCALATES rather than being silently skipped; (4) deploy_command refuses fc.
Named test_fp_* so the CI gate runs these on itself.
"""

from __future__ import annotations

import pytest

from ops.deploy_scope import (
    SERVICE_REGISTRY,
    DeployTier,
    affected_services,
    deploy_command,
)


def test_dashboard_change_is_tier1_auto() -> None:
    plan = affected_services(["services/dashboard/app.py"])
    assert plan.auto == ["dashboard"]
    assert plan.coordinated == []
    assert not plan.ignored
    assert not plan.needs_coordinated_relaunch


def test_frontend_change_maps_to_dashboard() -> None:
    plan = affected_services(["frontend/src/App.tsx"])
    assert plan.auto == ["dashboard"]


def test_feature_group_change_is_coordinated_never_auto() -> None:
    # The keystone safety property: a feature change can shift the fingerprint → coordinated relaunch ONLY.
    plan = affected_services(["quantlib/features/groups/momentum.py"])
    assert plan.auto == []
    assert plan.coordinated == ["feature-computer"]
    assert plan.needs_coordinated_relaunch


def test_rust_kernel_change_is_coordinated() -> None:
    plan = affected_services(["rust/src/kernel.rs"])
    assert plan.coordinated == ["feature-computer"]
    assert plan.auto == []


def test_bus_schema_change_is_coordinated() -> None:
    plan = affected_services(["quantlib/bus/schema.py"])
    assert plan.coordinated == ["feature-computer"]


def test_strategy_change_is_tier1() -> None:
    plan = affected_services(["services/strategies/reversion/strategy.py"])
    assert plan.auto == ["reversion-strategy"]


def test_news_and_edgar_changes_are_tier1() -> None:
    plan = affected_services(["services/news_capture/main.py", "services/edgar/poll.py"])
    assert set(plan.auto) == {"news-capture", "quant-edgar"}


def test_docs_and_tests_only_is_ignored() -> None:
    plan = affected_services(["docs/X.md", "tests/test_y.py", "README.md"])
    assert plan.ignored
    assert plan.auto == [] and plan.coordinated == []


def test_mixed_dashboard_and_feature_splits_by_tier() -> None:
    plan = affected_services(["services/dashboard/app.py", "quantlib/features/groups/x.py"])
    assert plan.auto == ["dashboard"]
    assert plan.coordinated == ["feature-computer"]
    assert plan.needs_coordinated_relaunch


def test_unknown_container_path_escalates() -> None:
    plan = affected_services(["services/brand_new_svc/main.py"])
    assert plan.unknown_paths == ["services/brand_new_svc/main.py"]
    assert plan.auto == [] and plan.coordinated == []


def test_ops_ci_change_routes_to_grade_daemon_no_container_op() -> None:
    # The grade daemon self-refreshes its CI checkout → no container op, but recorded in reasons.
    plan = affected_services(["ops/ci_watcher.py"])
    assert plan.auto == []  # pseudo-service dropped from container deploys
    assert plan.coordinated == []
    assert any("grade daemon" in reason for reason in plan.reasons)


def test_deploy_command_dashboard_is_compose_up_by_name() -> None:
    cmd = deploy_command("dashboard", "/home/ben/quant-fp")
    assert cmd == ["docker", "compose", "up", "-d", "--no-deps", "--build", "dashboard"]


def test_deploy_command_strategy_includes_overlay_compose_file() -> None:
    cmd = deploy_command("reversion-strategy", "/home/ben/quant-fp")
    assert "-f" in cmd and "docker-compose.strategies.yml" in cmd
    assert cmd[-3:] == ["--no-deps", "--build", "reversion-strategy"]


def test_deploy_command_refuses_fc() -> None:
    with pytest.raises(ValueError):
        deploy_command("feature-computer", "/home/ben/quant-fp")


def test_fc_is_registered_tier2() -> None:
    assert SERVICE_REGISTRY["feature-computer"].tier is DeployTier.COORDINATED
