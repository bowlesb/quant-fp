"""Unit tests for the CI gate's gating logic (ops.ci_watcher.SuiteResult).

The safety-critical property: a NON-GATING (timing / wall-clock) job failure must NOT red the gate — else
the gate false-reds legitimate PRs on a loaded box and gets ignored. Correctness failures + coverage blind
spots MUST red it. Named test_fp_* so the gate runs these on itself.
"""

from __future__ import annotations

from ops.ci_watcher import (
    DASHBOARD_DEP_TESTS,
    HARNESS_ORPHAN_TESTS,
    JobResult,
    SuiteResult,
    _FP_EXCLUDES,
    _KNOWN_COLLECTION_ERRORS,
    _parse_failed_ids,
    STORE_TEST_DIR,
)


def _green(name: str, gating: bool = True) -> JobResult:
    return JobResult(name=name, passed=True, tail="", gating=gating)


def _red(name: str, gating: bool = True) -> JobResult:
    return JobResult(name=name, passed=False, tail="boom", gating=gating)


def test_timing_flake_does_not_red_the_gate() -> None:
    # The Lead's exact scenario: volatility 37.3ms > 33ms under box load → timing job RED, gate stays GREEN.
    suite = SuiteResult(
        jobs=[_green("fp"), _green("dashboard"), _red("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is True


def test_parse_failed_ids_extracts_test_ids() -> None:
    # The xdist-flake retry parses pytest's `FAILED ...` summary (needs -rf) to know what to re-run isolated.
    output = (
        "..F...\n"
        "=== short test summary info ===\n"
        "FAILED tests/test_next_quote_tranche.py::test_compute_tranche_filters_band_and_ranks_by_headroom\n"
        "FAILED tests/test_foo.py::test_bar - AssertionError: 1 != 2\n"
        "1 failed, 99 passed\n"
    )
    assert _parse_failed_ids(output) == [
        "tests/test_next_quote_tranche.py::test_compute_tranche_filters_band_and_ranks_by_headroom",
        "tests/test_foo.py::test_bar",
    ]


def test_parse_failed_ids_empty_when_no_failures() -> None:
    assert _parse_failed_ids("99 passed in 2.10s\n") == []


def test_parse_failed_ids_dedups() -> None:
    output = "FAILED tests/a.py::test_x\nFAILED tests/a.py::test_x\n"
    assert _parse_failed_ids(output) == ["tests/a.py::test_x"]


def test_recovered_flake_does_not_red_the_fp_job() -> None:
    # An fp job that passed (because its parallel failures recovered isolated) carries flaky_recovered but
    # passed=True → the gate is GREEN. The recovered ids are reported, not blocking.
    fp = JobResult(
        name="fp",
        passed=True,
        tail="",
        gating=True,
        flaky_recovered=["tests/test_next_quote_tranche.py::test_compute_tranche_filters_band_and_ranks"],
    )
    suite = SuiteResult(jobs=[fp, _green("dashboard"), _green("store")], uncovered=[])
    assert suite.passed is True


def test_gating_job_failure_reds_the_gate() -> None:
    suite = SuiteResult(
        jobs=[_red("fp"), _green("dashboard"), _green("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is False


def test_store_job_failure_reds_the_gate() -> None:
    # The store job (tests/battery/ with the store mounted) is GATING — a real failure there must red.
    suite = SuiteResult(
        jobs=[_green("fp"), _green("dashboard"), _red("store"), _green("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is False


def test_dashboard_job_failure_reds_the_gate() -> None:
    suite = SuiteResult(
        jobs=[_green("fp"), _red("dashboard"), _green("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is False


def test_uncovered_blind_spot_reds_the_gate_even_when_all_jobs_green() -> None:
    suite = SuiteResult(
        jobs=[_green("fp"), _green("dashboard"), _green("timing", gating=False)],
        uncovered=["tests/test_new_blindspot.py"],
    )
    assert suite.passed is False


def test_all_green_no_blind_spots_is_green() -> None:
    suite = SuiteResult(
        jobs=[_green("fp"), _green("dashboard"), _green("store"), _green("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is True


def test_harness_orphan_is_a_known_collection_error_not_a_blind_spot() -> None:
    # The experimenter orphan ERRORs at collection in every repo env; the audit must treat it as KNOWN, not
    # flag it as a blind spot (which would red the gate forever).
    assert "tests/test_experimenter_transient.py" in HARNESS_ORPHAN_TESTS
    assert set(HARNESS_ORPHAN_TESTS) <= _KNOWN_COLLECTION_ERRORS
    assert set(DASHBOARD_DEP_TESTS) <= _KNOWN_COLLECTION_ERRORS


def test_fp_job_excludes_every_other_env_category() -> None:
    # The gating fp job must NOT run any test that needs a different env, else it false-reds.
    for path in (*DASHBOARD_DEP_TESTS, *HARNESS_ORPHAN_TESTS):
        assert path in _FP_EXCLUDES
    assert STORE_TEST_DIR in _FP_EXCLUDES
