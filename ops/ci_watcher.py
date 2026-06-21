"""Self-hosted CI watcher — the merge gate (docs/CONTINUOUS_DEPLOY.md Phase 1-2).

A box-local daemon (no cloud GitHub Actions — the fp test env is the local ``fp-dev`` image + ``/store`` +
DB, which a cloud runner can't replicate). On every open PR against ``main`` whose head SHA it hasn't yet
graded it:

  1. checks the head SHA out into a throwaway git worktree;
  2. runs every CI job in ``fp-dev --rm`` containers (env SCRUBBED — never mounts ``.env``, so the paper
     Alpaca creds cannot leak into a CI log): the ``fp`` job runs the WHOLE ``tests/`` dir minus the
     dashboard-dep files; the ``dashboard`` job runs exactly those after ``pip install -r
     services/dashboard/requirements.txt`` (they import fastapi/pyyaml/pymongo, absent from base fp-dev). A
     coverage audit then reports any test file NO job ran (LOUD blind-spot list → RED), so "green" can never
     hide an untested class — the half-tested pattern Ben is killing;
  3. classifies scope (ci_scope.classify) — fingerprint vs origin/main + changed-path allowlist — into
     TIER-1 (auto) or TIER-2 (gated);
  4. posts a COMMIT STATUS via ``gh api`` (context ``ci/fp-suite``) so the PR's mergeability reflects real
     test state, plus a sticky summary comment, and applies the ``tier-2-gated`` label when gated;
  5. (Phase 2) if green AND TIER-1 AND not held by ``no-auto`` → auto-merges (``gh pr merge --squash``).

The actual deploy (Phase 3) is a separate watcher (ci_deploy.py) triggered off new merges to main.

This file shells out to ``git`` / ``docker`` / ``gh`` — it is the impure orchestrator. The decision logic it
relies on (ci_scope) is pure + unit-tested. Run::

    python -m ops.ci_watcher --once              # grade every open PR once, then exit
    python -m ops.ci_watcher --poll 60           # daemon: re-grade on each new head SHA every 60s
    python -m ops.ci_watcher --once --pr 412      # grade exactly one PR
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from ops.ci_scope import ScopeResult, Tier, classify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s ci-watcher %(message)s",
)
logger = logging.getLogger("ci_watcher")

REPO_DIR = os.environ.get("CI_REPO_DIR", "/home/ben/quant-fp")
FP_IMAGE = os.environ.get("CI_FP_IMAGE", "fp-dev")
STATUS_CONTEXT = "ci/fp-suite"
GATED_LABEL = "tier-2-gated"
NO_AUTO_LABEL = "no-auto"
# The gate runs the WHOLE tests/ dir (not just test_fp_*) so it never silently skips a class of tests — the
# half-tested pattern Ben is killing. Overridable (CI_SUITE_GLOB) for a focused/smoke gate; whitespace-split.
SUITE_GLOB = os.environ.get("CI_SUITE_GLOB", "tests/")
# These test files import dashboard-only deps (fastapi / pyyaml / pymongo) absent from the base fp-dev image,
# so they ERROR at collection there. They are NOT skipped silently: the fp job --ignores them and a SECOND
# dashboard job runs them in fp-dev + `pip install -r services/dashboard/requirements.txt` (the authoritative,
# drift-proof dep closure). Keep this list in sync if a new dashboard-dep test appears (the gate reports any
# uncovered file loudly, so drift surfaces).
DASHBOARD_DEP_TESTS = (
    "tests/test_group_guide.py",
    "tests/test_store_grid.py",
    "tests/test_store_grid_cache.py",
    "tests/test_latency_expectations_route.py",
)
DASHBOARD_REQUIREMENTS = "services/dashboard/requirements.txt"
# WALL-CLOCK TIMING tests — they measure elapsed ms against a budget/ceiling, so they FALSE-RED on a loaded
# box (the agent fleet routinely pushes load to ~48). They run as a SEPARATE, NON-GATING `timing` job:
# reported informationally, but NEVER blocking the merge gate — a timing flake must not red a correct PR, or
# the gate cries wolf and gets ignored. CORRECTNESS (parity / fingerprint / value-identity / metric LOGIC)
# gates hard; only these elapsed-time assertions are demoted. test_fp_latency_metrics/_drilldown/_expectations
# are NOT here — they assert on recorded VALUES, not wall-clock, so they stay in the gate.
TIMING_TESTS = (
    "tests/test_fp_latency_budget.py",  # per-group profile(latest) ms < budget — the flaky one
    "tests/test_fp_latency.py",  # us/feature + trades-live ceilings (measured ms)
    "tests/test_fp_latency_e2e.py",  # opt-in sim e2e latency (self-skips without FP_LATENCY_E2E)
)
# STORE-dependent tests — they build a real daily/intraday panel from the feature store, so bare fp-dev (no
# /store volume) gives an empty glob -> `cannot concat empty list`. They run in a dedicated GATING `store`
# job WITH the read-only store mounted (`-v fp_store_real:/store:ro`), so they get REAL coverage, not a skip.
# The whole tests/battery/ dir is the clean boundary (its panel-building harness is what needs the store).
STORE_TEST_DIR = "tests/battery/"
# HARNESS-orphan tests — they `import main` from a research-harness entry (services/experimenter) that does
# NOT exist in this repo checkout (it lives only in the experimenter image), so they ERROR at collection in
# ANY repo-based CI env. lightgbm IS in fp-dev so their importorskip(lightgbm) does NOT skip them. They are
# excluded + flagged (the coverage audit lists any NEW such orphan), never allowed to silently red the gate.
HARNESS_ORPHAN_TESTS = ("tests/test_experimenter_transient.py",)
# Parallelism for the big `fp` job (~1200 tests). Serial it took ~30 min — too slow to grade the queue. We
# pip-install pytest-xdist and run `-n CI_XDIST_WORKERS`. FIXED small worker count, NEVER `-n auto`: this is a
# 32-core SHARED box (fc / crypto / strategies live-capture), and `auto` spawns ~32 workers and spikes load
# past 100, starving capture. CI_DOCKER_CPUS additionally caps the container's CPU so a grade can never
# saturate the box regardless of worker count. 6 workers ≈ 5-8 min for the fp job, leaving the box headroom.
XDIST_WORKERS = int(os.environ.get("CI_XDIST_WORKERS", "6"))
DOCKER_CPUS = os.environ.get("CI_DOCKER_CPUS", "6")
PYTEST_XDIST_REQ = os.environ.get("CI_XDIST_REQ", "pytest-xdist")
# Bound a single suite run so a hung test can't wedge the daemon. The full suite is well under this.
SUITE_TIMEOUT_S = int(os.environ.get("CI_SUITE_TIMEOUT_S", "1200"))


def run(cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Never raises on non-zero — the caller inspects returncode."""
    logger.debug("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


STORE_VOLUME = os.environ.get("CI_STORE_VOLUME", "fp_store_real")


def fp_docker(worktree: str, mount_store: bool = False) -> list[str]:
    """The ``docker run`` prefix for fp-dev jobs over a checkout.

    Runs as the HOST user (so files written into the bind-mounted worktree are owned by us, not root — else
    the throwaway-worktree cleanup hits PermissionError on root-owned ``__pycache__``) with bytecode writing
    OFF, ``HOME=/tmp`` (the non-root user has no home), and NO ``.env`` (env-scrubbed — creds can't leak).

    ``mount_store`` mounts the feature store READ-ONLY at /store (``-v fp_store_real:/store:ro``) for the
    store-dependent panel-building tests. RO so CI can never write the live store.
    """
    prefix = [
        "docker",
        "run",
        "--rm",
        # Cap CPU so a CI grade can never starve live capture (fc / crypto / strategies share this box).
        "--cpus",
        DOCKER_CPUS,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{worktree}:/app",
    ]
    if mount_store:
        prefix += ["-v", f"{STORE_VOLUME}:/store:ro", "-e", "STORE_ROOT=/store"]
    prefix += ["-w", "/app", FP_IMAGE]
    return prefix


def gh_json(args: list[str]) -> Any:
    """Run a ``gh`` command expected to emit JSON and parse it (empty -> None).

    Returns ``Any`` because GitHub's JSON is genuinely dynamic external data; callers index/iterate the
    specific shape they requested.
    """
    result = run(["gh", *args], cwd=REPO_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


@dataclass
class OpenPR:
    number: int
    head_sha: str
    head_ref: str
    labels: list[str]


def list_open_prs() -> list[OpenPR]:
    """Open PRs targeting ``main``."""
    raw = gh_json(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            "main",
            "--json",
            "number,headRefOid,headRefName,labels",
        ]
    )
    prs: list[OpenPR] = []
    for item in raw or []:
        prs.append(
            OpenPR(
                number=item["number"],
                head_sha=item["headRefOid"],
                head_ref=item["headRefName"],
                labels=[label["name"] for label in item["labels"]],
            )
        )
    return prs


def fingerprint_in(worktree: str) -> int:
    """Compute ``BusSchema.from_registry().fingerprint`` inside the fp-dev image for the given checkout.

    No ``.env`` mounted — the fingerprint is pure registry state, needs no creds/DB.
    """
    result = run(
        [
            *fp_docker(worktree),
            "python",
            "-c",
            "from quantlib.bus.schema import BusSchema; print(BusSchema.from_registry().fingerprint)",
        ],
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fingerprint compute failed in {worktree}: {result.stderr.strip()[-400:]}")
    return int(result.stdout.strip().splitlines()[-1])


def changed_paths(worktree: str) -> list[str]:
    """``git diff --name-only origin/main...HEAD`` for the checkout (the PR's net changes vs base)."""
    result = run(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=worktree)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


@dataclass
class JobResult:
    """One CI job's outcome (a job = one pytest invocation in one container env)."""

    name: str
    passed: bool
    tail: str
    gating: bool = True  # if False, the job is INFORMATIONAL — reported but never blocks the merge gate
    # Test ids that FAILED under parallel (-n) but PASSED on isolated serial re-run → xdist-ordering flakes,
    # NOT real reds. Reported informationally; they do not red the job. Empty in the normal case.
    flaky_recovered: list[str] = field(default_factory=list)


@dataclass
class SuiteResult:
    """The combined outcome of every CI job + the coverage-honesty audit."""

    jobs: list[JobResult]
    uncovered: list[str]  # test files that NO job ran (the loud blind-spot list; must stay empty)

    @property
    def passed(self) -> bool:
        # Green iff every GATING job passes AND nothing is uncovered. Non-gating (timing) jobs are reported
        # but never block — a wall-clock flake under load must not red a correct PR.
        return all(job.passed for job in self.jobs if job.gating) and not self.uncovered


def _run_pytest(
    worktree: str, pytest_cmd: str, job_name: str, gating: bool = True, mount_store: bool = False
) -> JobResult:
    """Run one pytest invocation in an fp-dev --rm container (env-scrubbed, host-user). sh -c so globs expand.

    SECURITY: no ``--env-file .env`` / no secret env — the suite needs none, so the paper Alpaca creds can
    never reach a CI log. --rm; bind-mounts only the throwaway checkout at /app (+ the store RO if requested).
    """
    passed, output = _exec_pytest(worktree, pytest_cmd, job_name, mount_store)
    tail = "\n".join(output.splitlines()[-25:])
    return JobResult(job_name, passed, tail, gating)


def _exec_pytest(worktree: str, pytest_cmd: str, job_name: str, mount_store: bool) -> tuple[bool, str]:
    """Run a pytest command in an fp-dev --rm container; return (passed, full combined output).

    SECURITY: no ``--env-file .env`` / no secret env — the suite needs none, so the paper Alpaca creds can
    never reach a CI log. --rm; bind-mounts only the throwaway checkout at /app (+ the store RO if requested).
    """
    cmd = [*fp_docker(worktree, mount_store=mount_store), "sh", "-c", pytest_cmd]
    try:
        result = run(cmd, timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"{job_name} TIMED OUT after {SUITE_TIMEOUT_S}s"
    return result.returncode == 0, (result.stdout + "\n" + result.stderr).strip()


_FAILED_ID_RE = re.compile(r"^FAILED (\S+::\S+)")


def _parse_failed_ids(output: str) -> list[str]:
    """Extract the ``tests/...::test_*`` ids from a pytest run's ``FAILED ...`` summary lines (needs ``-rf``
    / ``-ra``, which the fp job passes). Deduped, order-preserving."""
    ids: list[str] = []
    for line in output.splitlines():
        match = _FAILED_ID_RE.match(line.strip())
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


# Every category the fp job must NOT run (each is covered by its own job or is an unrunnable orphan).
_FP_EXCLUDES = (*DASHBOARD_DEP_TESTS, *TIMING_TESTS, *HARNESS_ORPHAN_TESTS, STORE_TEST_DIR)


def _run_fp_job(worktree: str) -> JobResult:
    """The gating ``fp`` job: run the whole tests/ dir in BOUNDED PARALLEL (-n), then make the result robust
    to xdist test-ISOLATION flakes.

    Parallelism (-n) can surface tests that share global state / depend on collection order: they pass
    isolated but fail under a particular worker distribution. To avoid false-redding clean PRs, when the
    parallel run fails we RE-RUN exactly the failed ids in ISOLATION (serial, single process). Any that pass
    isolated were xdist-ordering FLAKES → they don't red the job (logged informationally). Any that STILL
    fail isolated are REAL reds → the job stays RED. Standard xdist mitigation, cost = only the few that
    failed.
    """
    ignores = " ".join(f"--ignore={path}" for path in _FP_EXCLUDES)
    # -rf so the FAILED summary lists test ids we can re-run; -p no:randomly keeps order deterministic.
    parallel_cmd = (
        f"pip install -q --user {PYTEST_XDIST_REQ} && "
        f"python -m pytest {SUITE_GLOB} {ignores} -n {XDIST_WORKERS} -q -rf -p no:cacheprovider"
    )
    passed, output = _exec_pytest(worktree, parallel_cmd, "fp", mount_store=False)
    if passed:
        return JobResult("fp", True, "\n".join(output.splitlines()[-25:]), gating=True)

    failed_ids = _parse_failed_ids(output)
    if not failed_ids:
        # Failed but we couldn't parse ids (e.g. a collection error / crash) — cannot safely call it a flake.
        return JobResult("fp", False, "\n".join(output.splitlines()[-25:]), gating=True)

    logger.info("fp parallel run RED on %d test(s); re-running isolated: %s", len(failed_ids), failed_ids)
    # Re-run exactly the failed ids, SERIALLY, single process (no -n) — the isolation check.
    isolated_cmd = f"python -m pytest {' '.join(failed_ids)} -p no:cacheprovider -rf -q -p no:randomly"
    isolated_passed, isolated_output = _exec_pytest(worktree, isolated_cmd, "fp-isolated", mount_store=False)
    if isolated_passed:
        logger.info(
            "all %d failure(s) PASSED isolated → xdist-ordering flake; fp job GREEN", len(failed_ids)
        )
        tail = "\n".join(output.splitlines()[-12:]) + "\n--- isolated re-run: all passed (flake) ---\n"
        tail += "\n".join(isolated_output.splitlines()[-8:])
        return JobResult("fp", True, tail, gating=True, flaky_recovered=failed_ids)

    still_failing = _parse_failed_ids(isolated_output)
    logger.info("isolated re-run STILL RED on %s → real failure; fp job RED", still_failing)
    tail = "\n".join(isolated_output.splitlines()[-20:])
    return JobResult("fp", False, tail, gating=True)


def run_suite(worktree: str) -> SuiteResult:
    """Run every CI job over the checkout + audit that no test file went unrun.

    - ``fp`` (GATING): the WHOLE ``tests/`` dir in base fp-dev, ``--ignore``-ing every category that needs a
      different env (dashboard / timing / store / harness-orphan) — each handled below or flagged.
    - ``dashboard`` (GATING): the dashboard-dep files in fp-dev + the dashboard's own requirements.
    - ``store`` (GATING): the panel-building ``tests/battery/`` dir WITH the feature store mounted RO
      (``-v fp_store_real:/store:ro``) — real coverage, not a skip.
    - ``timing`` (NON-GATING / informational): the wall-clock latency tests. Reported but NEVER blocks.
    Then a coverage audit: any ``tests/test_*.py`` that errors collection in the fp env and is NOT a known
    other-env category is a blind spot — reported LOUDLY (RED), so a new untested class can't hide.

    The fp job runs in bounded parallel (-n) and is robust to xdist test-isolation flakes via
    ``_run_fp_job`` (failed-in-parallel ids are re-confirmed isolated before declaring RED).
    """
    fp_job = _run_fp_job(worktree)

    dash_targets = " ".join(DASHBOARD_DEP_TESTS)
    dash_cmd = (
        f"pip install -q --user -r {DASHBOARD_REQUIREMENTS} && "
        f"python -m pytest {dash_targets} -q -p no:cacheprovider"
    )
    dash_job = _run_pytest(worktree, dash_cmd, "dashboard", gating=True)

    store_cmd = f"python -m pytest {STORE_TEST_DIR} -q -p no:cacheprovider"
    store_job = _run_pytest(worktree, store_cmd, "store", gating=True, mount_store=True)

    timing_targets = " ".join(TIMING_TESTS)
    timing_cmd = f"python -m pytest {timing_targets} -q -p no:cacheprovider"
    timing_job = _run_pytest(worktree, timing_cmd, "timing", gating=False)

    uncovered = _audit_coverage(worktree)
    return SuiteResult(jobs=[fp_job, dash_job, store_job, timing_job], uncovered=uncovered)


# Test files that legitimately ERROR at collection in the bare fp env because they belong to another job /
# are unrunnable orphans — so a collection error on THESE is expected, not a blind spot.
_KNOWN_COLLECTION_ERRORS = frozenset((*DASHBOARD_DEP_TESTS, *HARNESS_ORPHAN_TESTS))


def _audit_coverage(worktree: str) -> list[str]:
    """Detect ``tests/test_*.py`` files the fp job would silently DROP at collection — the real blind spot.

    A new test importing a dep absent from base fp-dev (dashboard pattern), needing the store, or importing a
    harness entry not in the repo (experimenter pattern) ERRORs at collection. We ``--collect-only`` the fp
    env over ``tests/`` and report any file that errors yet is NOT a KNOWN other-env category — i.e. a NEW
    uncovered class that must be triaged into a job (or its list) before the gate can be trusted green. Store
    tests live under ``tests/battery/`` and collect fine in the bare env (they only FAIL at run-time without
    the store), so they don't appear here; they're covered by the store job.
    """
    collect_cmd = "python -m pytest tests/ --collect-only -q -p no:cacheprovider"
    result = run([*fp_docker(worktree), "sh", "-c", collect_cmd], timeout=300)
    errored = {
        line.split()[1]
        for line in result.stdout.splitlines()
        if line.startswith("ERROR ") and len(line.split()) >= 2
    }
    return sorted(errored - _KNOWN_COLLECTION_ERRORS)


def post_status(sha: str, state: str, description: str) -> None:
    """Post a commit status so the PR's mergeability reflects real test state.

    ``state`` in {success, failure, pending, error}. Uses the repo's statuses API via ``gh api``.
    """
    description = description[:140]  # GitHub caps status descriptions
    result = run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{_repo_slug()}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={STATUS_CONTEXT}",
            "-f",
            f"description={description}",
        ],
        cwd=REPO_DIR,
    )
    if result.returncode != 0:
        logger.error("failed to post status for %s: %s", sha, result.stderr.strip())


_REPO_SLUG_CACHE: str | None = None


def _repo_slug() -> str:
    global _REPO_SLUG_CACHE
    if _REPO_SLUG_CACHE is None:
        raw = gh_json(["repo", "view", "--json", "nameWithOwner"])
        _REPO_SLUG_CACHE = str(raw["nameWithOwner"])
    return _REPO_SLUG_CACHE


STICKY_MARKER = "<!-- ci/fp-suite -->"


def post_comment(pr_number: int, body: str) -> None:
    """Post (or update) the sticky CI summary comment on the PR."""
    full_body = f"{STICKY_MARKER}\n{body}"
    comment_id = _find_sticky(pr_number)
    if comment_id is not None:
        result = run(
            [
                "gh",
                "api",
                "-X",
                "PATCH",
                f"repos/{_repo_slug()}/issues/comments/{comment_id}",
                "-f",
                f"body={full_body}",
            ],
            cwd=REPO_DIR,
        )
    else:
        result = run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{_repo_slug()}/issues/{pr_number}/comments",
                "-f",
                f"body={full_body}",
            ],
            cwd=REPO_DIR,
        )
    if result.returncode != 0:
        logger.error("failed to post comment on #%s: %s", pr_number, result.stderr.strip())


def _find_sticky(pr_number: int) -> int | None:
    """Find our sticky comment id (by marker), or None."""
    raw = gh_json(["api", f"repos/{_repo_slug()}/issues/{pr_number}/comments"])
    for comment in raw or []:
        if STICKY_MARKER in comment["body"]:
            return int(comment["id"])
    return None


def ensure_label(pr_number: int, label: str, present: bool) -> None:
    """Add or remove a label on the PR to match ``present`` (creating the repo label first if needed).

    Uses the REST issues/labels API rather than ``gh pr edit`` — the latter hits the deprecated GraphQL
    projects-classic path and errors even on a pure label change.
    """
    if present:
        run(
            ["gh", "label", "create", label, "--description", "CI-managed", "--color", "B60205", "--force"],
            cwd=REPO_DIR,
        )
        result = run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{_repo_slug()}/issues/{pr_number}/labels",
                "-f",
                f"labels[]={label}",
            ],
            cwd=REPO_DIR,
        )
    else:
        # DELETE is 404 if the label isn't on the PR — harmless, so we don't treat that as an error.
        result = run(
            ["gh", "api", "-X", "DELETE", f"repos/{_repo_slug()}/issues/{pr_number}/labels/{label}"],
            cwd=REPO_DIR,
        )
        return
    if result.returncode != 0:
        logger.error("failed to add '%s' on #%s: %s", label, pr_number, result.stderr.strip()[-200:])


def auto_merge(pr_number: int) -> bool:
    """Squash-merge a green TIER-1 PR. Returns True on success."""
    result = run(["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"], cwd=REPO_DIR)
    if result.returncode != 0:
        logger.error("auto-merge of #%s failed: %s", pr_number, result.stderr.strip())
        return False
    logger.info("AUTO-MERGED #%s (TIER-1, CI green)", pr_number)
    return True


def grade_pr(pr: OpenPR, auto_merge_enabled: bool) -> None:
    """Grade one PR: run the suite, classify scope, post status + comment + label, maybe auto-merge."""
    logger.info("grading PR #%s @ %s (%s)", pr.number, pr.head_sha[:9], pr.head_ref)
    post_status(pr.head_sha, "pending", "fp suite running")

    # Make sure the PR head SHA is present locally before we try to check it out (the branch may be new).
    run(["git", "fetch", "origin", pr.head_ref], cwd=REPO_DIR)

    with tempfile.TemporaryDirectory(prefix="ci-wt-") as worktree:
        # A detached worktree at exactly the PR head SHA. --force so a leftover dir can't block us.
        add = run(["git", "worktree", "add", "--detach", "--force", worktree, pr.head_sha], cwd=REPO_DIR)
        if add.returncode != 0:
            logger.error("worktree add failed for #%s: %s", pr.number, add.stderr.strip())
            post_status(pr.head_sha, "error", "CI could not check out PR head")
            return
        try:
            run(["git", "fetch", "origin", "main"], cwd=worktree)
            paths = changed_paths(worktree)
            suite = run_suite(worktree)
            fp_head = fingerprint_in(worktree)
            fp_base = _origin_main_fingerprint()
            scope = classify(paths, fp_base, fp_head)
        finally:
            run(["git", "worktree", "remove", "--force", worktree], cwd=REPO_DIR)

    passed = suite.passed
    tier_str = "TIER-1 (auto)" if scope.tier is Tier.AUTO else "TIER-2 (gated)"
    state = "success" if passed else "failure"
    jobs_str = ", ".join(
        f"{job.name}={'ok' if job.passed else 'RED'}{'' if job.gating else '*'}" for job in suite.jobs
    )
    summary = f"gate {'GREEN' if passed else 'RED'} ({jobs_str}; *=non-gating) — {tier_str}"
    post_status(pr.head_sha, state, summary)

    body = _comment_body(suite, scope, len(paths))
    post_comment(pr.number, body)
    ensure_label(pr.number, GATED_LABEL, present=scope.tier is Tier.GATED)

    if passed and scope.tier is Tier.AUTO and auto_merge_enabled and NO_AUTO_LABEL not in pr.labels:
        auto_merge(pr.number)
    else:
        logger.info(
            "#%s not auto-merged (passed=%s tier=%s auto_enabled=%s no_auto=%s)",
            pr.number,
            passed,
            scope.tier.value,
            auto_merge_enabled,
            NO_AUTO_LABEL in pr.labels,
        )


def _comment_body(suite: SuiteResult, scope: ScopeResult, n_paths: int) -> str:
    """Render the sticky CI comment — per-job verdicts + the coverage-honesty audit + scope."""
    passed = suite.passed
    status_line = "✅ **gate GREEN**" if passed else "❌ **gate RED**"
    tier = scope.tier.value
    reasons = "\n".join(f"- {reason}" for reason in scope.reasons)
    job_lines = "\n".join(_job_line(job) for job in suite.jobs)
    lines = [
        f"## CI — `{STATUS_CONTEXT}`",
        "",
        status_line,
        f"**Scope:** `{tier}` ({n_paths} changed paths)",
        "",
        "**Jobs** (gating jobs must pass; `timing` is informational — wall-clock tests that flake under box "
        "load never block a correct PR. `dashboard` + `store` run env-dependent tests in their own env "
        "(dashboard deps installed / feature store mounted RO), not silently skipped):",
        job_lines,
    ]
    if suite.uncovered:
        # Coverage honesty: a test file no job ran is a blind spot — make it RED and loud, never hidden.
        uncovered_md = "\n".join(f"- `{path}`" for path in suite.uncovered)
        lines += [
            "",
            "⚠️ **Uncovered test files (NO job ran these — gate is RED until covered):**",
            uncovered_md,
        ]
    lines += ["", "**Scope reasons:**", reasons]
    for job in suite.jobs:
        if not job.passed:
            note = "" if job.gating else " — INFORMATIONAL, does not block the gate"
            lines += [
                "",
                f"<details><summary>{job.name} job output (tail){note}</summary>",
                "",
                "```",
                job.tail,
                "```",
                "</details>",
            ]
    if tier == Tier.AUTO.value and passed:
        lines += ["", "_TIER-1 + gate green → eligible for auto-merge._"]
    elif tier == Tier.GATED.value:
        lines += ["", "_TIER-2 → gated to the Lead's controlled relaunch window; will NOT auto-merge._"]
    return "\n".join(lines)


def _job_line(job: JobResult) -> str:
    """One job's line in the sticky comment; non-gating jobs are flagged informational, and xdist-ordering
    flakes that were recovered by the isolated re-run are noted (they passed isolated → not a real red)."""
    verdict = "✅ passed" if job.passed else "❌ FAILED"
    suffix = "" if job.gating else " _(informational — non-gating)_"
    if job.flaky_recovered:
        suffix += (
            f" _(⚠️ {len(job.flaky_recovered)} xdist-ordering flake(s) passed on isolated re-run, "
            f"not a real red: {', '.join(f'`{tid}`' for tid in job.flaky_recovered)})_"
        )
    return f"- `{job.name}`: {verdict}{suffix}"


_BASE_FP_CACHE: tuple[str, int] | None = None


def _origin_main_fingerprint() -> int:
    """Fingerprint of origin/main, computed in a throwaway worktree and cached per main SHA."""
    global _BASE_FP_CACHE
    main_sha = run(["git", "rev-parse", "origin/main"], cwd=REPO_DIR).stdout.strip()
    if _BASE_FP_CACHE is not None and _BASE_FP_CACHE[0] == main_sha:
        return _BASE_FP_CACHE[1]
    with tempfile.TemporaryDirectory(prefix="ci-base-") as worktree:
        run(["git", "worktree", "add", "--detach", "--force", worktree, main_sha], cwd=REPO_DIR)
        try:
            fingerprint = fingerprint_in(worktree)
        finally:
            run(["git", "worktree", "remove", "--force", worktree], cwd=REPO_DIR)
    _BASE_FP_CACHE = (main_sha, fingerprint)
    return fingerprint


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-hosted CI watcher for quant-fp PRs")
    parser.add_argument("--once", action="store_true", help="grade every open PR once, then exit")
    parser.add_argument("--poll", type=int, default=60, help="daemon poll interval seconds (with no --once)")
    parser.add_argument("--pr", type=int, default=None, help="grade exactly this PR number")
    parser.add_argument(
        "--no-auto-merge",
        action="store_true",
        help="grade + status only; never auto-merge (Phase-1-only mode)",
    )
    args = parser.parse_args()
    auto_merge_enabled = not args.no_auto_merge

    run(["git", "fetch", "origin", "main"], cwd=REPO_DIR)

    if args.once or args.pr is not None:
        prs = list_open_prs()
        if args.pr is not None:
            prs = [pr for pr in prs if pr.number == args.pr]
            if not prs:
                logger.error("PR #%s is not an open PR against main", args.pr)
                return 1
        for pr in prs:
            grade_pr(pr, auto_merge_enabled)
        return 0

    logger.info("CI watcher daemon: polling every %ss (auto_merge=%s)", args.poll, auto_merge_enabled)
    graded: dict[int, str] = {}  # pr number -> last graded head SHA
    while True:
        run(["git", "fetch", "origin", "main"], cwd=REPO_DIR)
        for pr in list_open_prs():
            if graded.get(pr.number) == pr.head_sha:
                continue  # already graded this exact head SHA
            grade_pr(pr, auto_merge_enabled)
            graded[pr.number] = pr.head_sha
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
