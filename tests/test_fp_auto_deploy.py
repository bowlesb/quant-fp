"""Tests for the auto-deploy FF-fp-safety guard (ops/auto_deploy.py).

The guard's job: the continuous deployer FFs the WHOLE fc bind-mount tree before a TIER-1 rebuild, so it
must REFUSE that FF whenever origin/main carries an un-deployed fc/fingerprint-surface change — otherwise a
dashboard deploy would silently advance the fc fingerprint tree on disk, applying a fingerprint change at the
next relaunch that never went through the Ben-gated coordinated-deploy decision. These cover the pure decision
core (``_ff_safe_from_changed_paths``), which is offline-testable.
"""

from __future__ import annotations

from ops.auto_deploy import _ff_safe_from_changed_paths


def test_ff_safe_when_no_changes() -> None:
    safe, blocking = _ff_safe_from_changed_paths([])
    assert safe is True
    assert blocking == []


def test_ff_safe_for_pure_tier1_and_neutral_paths() -> None:
    paths = [
        "services/dashboard/app.py",
        "frontend/src/Grid.jsx",
        "docs/AUTO_DEPLOY.md",
        "tests/test_fp_deploy_scope.py",
        "ops/auto_deploy.py",
    ]
    safe, blocking = _ff_safe_from_changed_paths(paths)
    assert safe is True
    assert blocking == []


def test_ff_unsafe_when_feature_code_pending() -> None:
    safe, blocking = _ff_safe_from_changed_paths(["quantlib/features/groups/price_volume.py"])
    assert safe is False
    assert "feature-computer" in blocking


def test_ff_unsafe_for_rust_kernel_change() -> None:
    safe, blocking = _ff_safe_from_changed_paths(["rust/src/reduce.rs"])
    assert safe is False
    assert "feature-computer" in blocking


def test_ff_unsafe_for_bus_schema_change() -> None:
    safe, blocking = _ff_safe_from_changed_paths(["quantlib/bus/schema.py"])
    assert safe is False
    assert "feature-computer" in blocking


def test_ff_unsafe_when_tier1_and_tier2_both_pending() -> None:
    # A dashboard change alone would be safe, but a co-pending feature change must block the whole-tree FF.
    paths = ["services/dashboard/app.py", "quantlib/features/groups/distribution.py"]
    safe, blocking = _ff_safe_from_changed_paths(paths)
    assert safe is False
    assert "feature-computer" in blocking
