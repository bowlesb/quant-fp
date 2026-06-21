"""Unit tests for the CI gate's gating logic (ops.ci_watcher.SuiteResult).

The safety-critical property: a NON-GATING (timing / wall-clock) job failure must NOT red the gate — else
the gate false-reds legitimate PRs on a loaded box and gets ignored. Correctness failures + coverage blind
spots MUST red it. Named test_fp_* so the gate runs these on itself.
"""

from __future__ import annotations

from ops.ci_watcher import JobResult, SuiteResult


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


def test_gating_job_failure_reds_the_gate() -> None:
    suite = SuiteResult(
        jobs=[_red("fp"), _green("dashboard"), _green("timing", gating=False)],
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
        jobs=[_green("fp"), _green("dashboard"), _green("timing", gating=False)],
        uncovered=[],
    )
    assert suite.passed is True
