"""Unit tests for the per-checkout test-env policy loader (ops.ci_watcher.load_policy).

The bug this guards: the env-classification lists (which tests need dashboard deps / store / are
timing / orphans) MUST come from the PR CHECKOUT, not the daemon's possibly-stale running module — else a
stale daemon false-reds (or false-greens) a clean PR. The loader parses them from the checkout's source via
AST (no code execution), with a safe fallback. Named test_fp_* so the gate runs these on itself.
"""

from __future__ import annotations

import os

import pytest

from ops.ci_watcher import (
    _DEFAULT_POLICY,
    TestEnvPolicy,
    _augment_with_autodetected_dashboard_tests,
    _dashboard_dep_modules,
    _literal_str_tuple,
    _parse_policy_constants,
    load_policy,
)

_GOOD_SOURCE = """
DASHBOARD_DEP_TESTS = (
    "tests/test_group_guide.py",
    "tests/test_news_edgar_route.py",
)
TIMING_TESTS = ("tests/test_fp_latency.py",)
HARNESS_ORPHAN_TESTS = ("tests/test_experimenter_transient.py",)
STORE_TEST_DIR = "tests/battery/"
"""


def test_parse_policy_constants_extracts_all() -> None:
    parsed = _parse_policy_constants(_GOOD_SOURCE)
    assert parsed["DASHBOARD_DEP_TESTS"] == (
        "tests/test_group_guide.py",
        "tests/test_news_edgar_route.py",
    )
    assert parsed["TIMING_TESTS"] == ("tests/test_fp_latency.py",)
    assert parsed["STORE_TEST_DIR"] == ("tests/battery/",)


def test_parse_policy_missing_constant_raises() -> None:
    # Missing STORE_TEST_DIR → ValueError (caller falls back to default).
    source = "DASHBOARD_DEP_TESTS = ()\nTIMING_TESTS = ()\nHARNESS_ORPHAN_TESTS = ()\n"
    with pytest.raises(ValueError):
        _parse_policy_constants(source)


def test_literal_str_tuple_forms() -> None:
    import ast

    assert _literal_str_tuple(ast.parse('"x"', mode="eval").body) == ("x",)
    assert _literal_str_tuple(ast.parse('("a", "b")', mode="eval").body) == ("a", "b")
    with pytest.raises(ValueError):
        _literal_str_tuple(ast.parse("(1, 2)", mode="eval").body)  # non-string elements


def test_load_policy_reads_news_edgar_from_checkout(tmp_path: object) -> None:
    # The exact stale-daemon scenario: a checkout whose list INCLUDES test_news_edgar_route.py must yield a
    # policy that excludes it from the fp job — regardless of what the daemon's own module says.
    ops_dir = os.path.join(str(tmp_path), "ops")
    os.makedirs(ops_dir)
    with open(os.path.join(ops_dir, "ci_watcher.py"), "w") as handle:
        handle.write(_GOOD_SOURCE)
    policy = load_policy(str(tmp_path))
    assert "tests/test_news_edgar_route.py" in policy.dashboard_dep_tests
    assert "tests/test_news_edgar_route.py" in policy.fp_excludes
    assert "tests/test_news_edgar_route.py" in policy.known_collection_errors


def test_load_policy_falls_back_when_no_file(tmp_path: object) -> None:
    # An empty checkout (no ops/ci_watcher.py) → the daemon's default policy, not a crash.
    policy = load_policy(str(tmp_path))
    assert policy == _DEFAULT_POLICY


def test_default_policy_excludes_known_dashboard_and_orphan() -> None:
    pol = _DEFAULT_POLICY
    assert "tests/test_news_edgar_route.py" in pol.fp_excludes
    assert "tests/test_experimenter_transient.py" in pol.fp_excludes
    assert pol.store_test_dir in pol.fp_excludes
    assert "tests/test_experimenter_transient.py" in pol.known_collection_errors


def test_policy_is_frozen() -> None:
    pol = TestEnvPolicy(("a",), ("b",), ("c",), "tests/battery/")
    with pytest.raises(Exception):
        pol.dashboard_dep_tests = ("x",)  # type: ignore[misc]


def test_status_grid_is_registered_dashboard_dep() -> None:
    # The whack-a-mole regression: #382's tests/test_status_grid.py imports status_grid -> filelock.
    assert "tests/test_status_grid.py" in _DEFAULT_POLICY.dashboard_dep_tests
    assert "tests/test_status_grid.py" in _DEFAULT_POLICY.fp_excludes


def test_dashboard_dep_modules_parses_requirements(tmp_path: object) -> None:
    # The durable auto-detect's input: import-module names of the dashboard-only deps, normalised, with
    # base fp-dev modules (polars/numpy) excluded and aliases (pyyaml->yaml, [extras] stripped) applied.
    req_dir = os.path.join(str(tmp_path), "services", "dashboard")
    os.makedirs(req_dir)
    with open(os.path.join(req_dir, "requirements.txt"), "w") as handle:
        handle.write("# comment\nfastapi==0.115.6\nuvicorn[standard]==0.34.0\npyyaml>=6,<7\n")
        handle.write("filelock>=3.13,<4\npolars\nnumpy\npsycopg[binary]==3.2.3\n")
    modules = _dashboard_dep_modules(str(tmp_path))
    assert modules == frozenset({"fastapi", "uvicorn", "yaml", "filelock", "psycopg"})
    assert "polars" not in modules and "numpy" not in modules  # in base fp-dev, not dashboard-only


def test_dashboard_dep_modules_empty_when_no_requirements(tmp_path: object) -> None:
    # No requirements file → empty set → auto-detect no-ops (never makes grading stricter).
    assert _dashboard_dep_modules(str(tmp_path)) == frozenset()


def test_augment_is_noop_when_nothing_detected(monkeypatch: object) -> None:
    # When auto-detect finds no NEW dashboard tests, the policy is returned UNCHANGED (identity).
    import ops.ci_watcher as w

    monkeypatch.setattr(w, "_autodetect_dashboard_tests", lambda wt, mods: [])  # type: ignore[attr-defined]
    pol = _DEFAULT_POLICY
    assert _augment_with_autodetected_dashboard_tests("/nonexistent", pol) is pol


def test_augment_merges_new_detected_test(monkeypatch: object) -> None:
    # A detected dashboard test NOT in the static list is merged into dashboard_dep_tests (→ fp --ignores it).
    import ops.ci_watcher as w

    monkeypatch.setattr(
        w, "_autodetect_dashboard_tests", lambda wt, mods: ["tests/test_brand_new_dash.py"]
    )  # type: ignore[attr-defined]
    augmented = _augment_with_autodetected_dashboard_tests("/wt", _DEFAULT_POLICY)
    assert "tests/test_brand_new_dash.py" in augmented.dashboard_dep_tests
    assert "tests/test_brand_new_dash.py" in augmented.fp_excludes
    # already-registered tests are not duplicated
    monkeypatch.setattr(
        w, "_autodetect_dashboard_tests", lambda wt, mods: ["tests/test_status_grid.py"]
    )  # type: ignore[attr-defined]
    assert _augment_with_autodetected_dashboard_tests("/wt", _DEFAULT_POLICY) is _DEFAULT_POLICY
