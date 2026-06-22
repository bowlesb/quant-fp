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
import ast
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ops.ci_scope import ScopeResult, Tier, classify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s ci-watcher %(message)s",
)
logger = logging.getLogger("ci_watcher")

REPO_DIR = os.environ.get("CI_REPO_DIR", "/home/ben/quant-fp")
FP_IMAGE = os.environ.get("CI_FP_IMAGE", "fp-dev")
# Base dir for the watcher's throwaway PR-checkout worktrees. MUST be a STABLE path, NOT /tmp: the agent
# harness garbage-collects /tmp and would delete an in-flight grade's worktree out from under it (killing the
# grade + leaving a stuck `pending` status). Defaults to ``<CI_REPO_DIR>/../.ci-work`` (next to the repo, same
# filesystem so ``git worktree add`` is cheap). Override with CI_WORKDIR.
WORKDIR_BASE = os.environ.get("CI_WORKDIR") or os.path.join(
    os.path.dirname(os.path.abspath(REPO_DIR)), ".ci-work"
)
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
    "tests/test_news_edgar_route.py",
    "tests/test_status_grid.py",  # imports status_grid -> filelock (a dashboard-only dep)
    "tests/test_lifecycle_state.py",  # route tests import app -> fastapi/pymongo (dashboard-only deps)
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


@dataclass(frozen=True)
class TestEnvPolicy:
    """The per-env test classification (which tests need dashboard deps / store / are timing / orphans).

    CRITICAL: this is loaded from the PR CHECKOUT being graded, not the daemon's (possibly stale) running
    module. The daemon's live tree can lag origin/main (a daemon started at SHA X keeps X's lists in memory);
    if a PR adds a dashboard-dep test the stale daemon wouldn't ``--ignore`` it → fastapi collection error →
    false-RED a clean PR (or, the other way, false-GREEN). The exclusion policy must match the tree whose
    tests we run, so we parse it from the checkout. The module constants below are only the fallback default.
    """

    __test__ = False  # not a pytest test class despite the "Test" prefix

    dashboard_dep_tests: tuple[str, ...]
    timing_tests: tuple[str, ...]
    harness_orphan_tests: tuple[str, ...]
    store_test_dir: str

    @property
    def fp_excludes(self) -> tuple[str, ...]:
        """Everything the gating fp job must NOT run (each covered by its own job or an unrunnable orphan)."""
        return (
            *self.dashboard_dep_tests,
            *self.timing_tests,
            *self.harness_orphan_tests,
            self.store_test_dir,
        )

    @property
    def known_collection_errors(self) -> frozenset[str]:
        """Files that legitimately ERROR at collection in the bare fp env (other-env / orphan) — NOT blind
        spots. A collection error outside this set is a genuine new uncovered class."""
        return frozenset((*self.dashboard_dep_tests, *self.harness_orphan_tests))


# The fallback policy = this module's own constants (used when a checkout can't be parsed, e.g. an older PR).
_DEFAULT_POLICY = TestEnvPolicy(
    dashboard_dep_tests=DASHBOARD_DEP_TESTS,
    timing_tests=TIMING_TESTS,
    harness_orphan_tests=HARNESS_ORPHAN_TESTS,
    store_test_dir=STORE_TEST_DIR,
)

# Constants read from the checkout's ops/ci_watcher.py source (tuple-of-str / str literals only).
_POLICY_TUPLE_NAMES = ("DASHBOARD_DEP_TESTS", "TIMING_TESTS", "HARNESS_ORPHAN_TESTS")
_POLICY_STR_NAMES = ("STORE_TEST_DIR",)


def load_policy(worktree: str) -> TestEnvPolicy:
    """Parse the env-classification lists from the CHECKOUT's ``ops/ci_watcher.py`` via AST (NO code
    execution — we never import the checkout's code). Falls back to the daemon's own constants if the file is
    missing or any constant can't be parsed, so an older PR (predating a list) still grades sanely."""
    source_path = os.path.join(worktree, "ops", "ci_watcher.py")
    if not os.path.isfile(source_path):
        logger.warning("checkout has no ops/ci_watcher.py — using the daemon's default test-env policy")
        return _DEFAULT_POLICY
    with open(source_path) as handle:
        source = handle.read()
    try:
        parsed = _parse_policy_constants(source)
    except (SyntaxError, ValueError) as exc:
        logger.warning("could not parse test-env policy from checkout (%s) — using default", exc)
        return _DEFAULT_POLICY
    return TestEnvPolicy(
        dashboard_dep_tests=parsed["DASHBOARD_DEP_TESTS"],
        timing_tests=parsed["TIMING_TESTS"],
        harness_orphan_tests=parsed["HARNESS_ORPHAN_TESTS"],
        store_test_dir=parsed["STORE_TEST_DIR"][0],
    )


def _parse_policy_constants(source: str) -> dict[str, tuple[str, ...]]:
    """Extract the policy constants as tuples of string literals from module source via AST. Raises
    ValueError if any required name is absent or not a pure str/tuple-of-str literal."""
    tree = ast.parse(source)
    found: dict[str, tuple[str, ...]] = {}
    wanted = set(_POLICY_TUPLE_NAMES) | set(_POLICY_STR_NAMES)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in wanted:
            continue
        found[target.id] = _literal_str_tuple(node.value)
    missing = wanted - set(found)
    if missing:
        raise ValueError(f"checkout policy missing constants: {sorted(missing)}")
    return found


def _literal_str_tuple(value: ast.expr) -> tuple[str, ...]:
    """A str literal -> 1-tuple; a tuple/list of str literals -> that tuple. Anything else raises."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, (ast.Tuple, ast.List)):
        items: list[str] = []
        for element in value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise ValueError("non-string element in policy tuple")
            items.append(element.value)
        return tuple(items)
    raise ValueError("policy constant is not a str / tuple-of-str literal")


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


def workdir(prefix: str) -> tempfile.TemporaryDirectory:
    """A throwaway temp dir for a PR-checkout worktree, rooted at the STABLE ``WORKDIR_BASE`` (never /tmp, which
    the agent harness GCs out from under an in-flight grade). Creates the base on first use."""
    os.makedirs(WORKDIR_BASE, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=WORKDIR_BASE)


def _warn_if_unstable_workdir() -> None:
    """Loudly warn at startup if the repo or the work base sits under /tmp — those get garbage-collected by
    the harness and will kill in-flight grades. Warn, don't abort (an operator may have a non-/tmp tmpfs)."""
    for label, path in (("CI_REPO_DIR", REPO_DIR), ("CI_WORKDIR base", WORKDIR_BASE)):
        if os.path.abspath(path).startswith("/tmp/"):
            logger.warning(
                "%s is under /tmp (%s) — the agent harness GCs /tmp and will kill in-flight grades. "
                "Point it at a stable path (e.g. /home/ben/...).",
                label,
                path,
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


# Back-compat: the default policy's fp-excludes (the daemon's own constants). run_suite uses the per-checkout
# policy instead; this name is kept for any external reference / tests.
_FP_EXCLUDES = _DEFAULT_POLICY.fp_excludes


def _run_fp_job(worktree: str, policy: TestEnvPolicy) -> JobResult:
    """The gating ``fp`` job: run the whole tests/ dir in BOUNDED PARALLEL (-n), then make the result robust
    to xdist test-ISOLATION flakes.

    Parallelism (-n) can surface tests that share global state / depend on collection order: they pass
    isolated but fail under a particular worker distribution. To avoid false-redding clean PRs, when the
    parallel run fails we RE-RUN exactly the failed ids in ISOLATION (serial, single process). Any that pass
    isolated were xdist-ordering FLAKES → they don't red the job (logged informationally). Any that STILL
    fail isolated are REAL reds → the job stays RED. Standard xdist mitigation, cost = only the few that
    failed.
    """
    ignores = " ".join(f"--ignore={path}" for path in policy.fp_excludes)
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
        # Failed with NO parseable FAILED ids — a collection error / xdist worker crash, which under heavy box
        # load can be a transient (a dropped worker, an --ignore not applied on a crashed shard). Before
        # false-redding a clean PR, do ONE SERIAL full re-run (no -n) — no xdist workers to crash. If it
        # passes, it was a parallel-infra transient → GREEN; if it still fails, it's real → RED.
        logger.info("fp parallel run RED with no parseable ids (collection/crash) — one serial re-run")
        serial_cmd = f"python -m pytest {SUITE_GLOB} {ignores} -q -rf -p no:cacheprovider -p no:randomly"
        serial_passed, serial_output = _exec_pytest(worktree, serial_cmd, "fp-serial", mount_store=False)
        if serial_passed:
            logger.info("serial re-run PASSED → parallel-infra transient; fp job GREEN")
            tail = (
                "\n".join(output.splitlines()[-10:]) + "\n--- serial re-run: passed (parallel transient) ---"
            )
            return JobResult("fp", True, tail, gating=True, flaky_recovered=["<parallel-infra transient>"])
        # Serial also failed: try to recover any now-parseable ids, else it's a genuine RED.
        serial_failed_ids = _parse_failed_ids(serial_output)
        if not serial_failed_ids:
            logger.info("serial re-run STILL RED with no parseable ids → genuine collection failure; fp RED")
            return JobResult("fp", False, "\n".join(serial_output.splitlines()[-25:]), gating=True)
        output, failed_ids = serial_output, serial_failed_ids

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

    The per-env test classification is loaded from the CHECKOUT (``load_policy``), NOT the daemon's possibly
    stale running module — so a PR that adds a dashboard-dep / store / orphan test is classified by ITS OWN
    lists, never false-redded by a lagging daemon.
    """
    policy = load_policy(worktree)
    # DURABLE auto-detect: any test that ERRORs at collection in the bare fp env because it imports a
    # dashboard-only dep (filelock/fastapi/pyyaml/pymongo/...) is auto-routed to the dashboard job, even if a
    # PR forgot to add it to DASHBOARD_DEP_TESTS. This kills the recurring whack-a-mole false-RED (#status_grid
    # /#news_edgar/...): a new dashboard test never reds the gate again. The static list stays as documentation
    # + the fallback when the requirements file is unreadable.
    policy = _augment_with_autodetected_dashboard_tests(worktree, policy)

    fp_job = _run_fp_job(worktree, policy)

    dash_targets = " ".join(policy.dashboard_dep_tests)
    dash_cmd = (
        f"pip install -q --user -r {DASHBOARD_REQUIREMENTS} && "
        f"python -m pytest {dash_targets} -q -p no:cacheprovider"
    )
    dash_job = _run_pytest(worktree, dash_cmd, "dashboard", gating=True)

    store_cmd = f"python -m pytest {policy.store_test_dir} -q -p no:cacheprovider"
    store_job = _run_pytest(worktree, store_cmd, "store", gating=True, mount_store=True)

    timing_targets = " ".join(policy.timing_tests)
    timing_cmd = f"python -m pytest {timing_targets} -q -p no:cacheprovider"
    timing_job = _run_pytest(worktree, timing_cmd, "timing", gating=False)

    uncovered = _audit_coverage(worktree, policy)
    return SuiteResult(jobs=[fp_job, dash_job, store_job, timing_job], uncovered=uncovered)


# Modules that base fp-dev HAS — a ModuleNotFoundError for one of these is NOT a dashboard-dep signal (it's a
# real broken import). Only deps that live solely in services/dashboard/requirements.txt mark a test as
# dashboard-only. polars/numpy are in both, so they're excluded from the dashboard-only set in code below.
_FP_DEV_BASE_MODULES = frozenset({"polars", "numpy"})

# requirement line -> import module name where they differ (most match after normalising extras/specifiers).
_REQ_IMPORT_ALIASES = {"pyyaml": "yaml", "psycopg[binary]": "psycopg", "uvicorn[standard]": "uvicorn"}


def _dashboard_dep_modules(worktree: str) -> frozenset[str]:
    """Import-module names of the dashboard-only deps (parsed from services/dashboard/requirements.txt).

    A test whose bare-fp collection error is ``No module named '<one of these>'`` is dashboard-dep by
    construction. Returns an empty set (→ auto-detect no-ops, static list still applies) if the file is
    unreadable, so this can never make grading stricter than before."""
    req_path = os.path.join(worktree, DASHBOARD_REQUIREMENTS)
    if not os.path.isfile(req_path):
        return frozenset()
    modules: set[str] = set()
    with open(req_path) as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # strip version/specifier: take the package token before any of <=>!~[ space
            name = re.split(r"[<>=!~ ]", line, maxsplit=1)[0].strip().lower()
            module = _REQ_IMPORT_ALIASES.get(name, re.sub(r"\[.*\]$", "", name))
            if module and module not in _FP_DEV_BASE_MODULES:
                modules.add(module)
    return frozenset(modules)


_MODULE_NOT_FOUND_RE = re.compile(r"No module named '([^']+)'")


def _autodetect_dashboard_tests(worktree: str, dashboard_modules: frozenset[str]) -> list[str]:
    """Tests under tests/ that ERROR at bare-fp collection because they import a dashboard-only module.

    Runs one ``--collect-only`` in the bare fp env capturing per-file error text, and keeps the ``tests/*.py``
    whose error is a ``No module named '<dashboard-dep>'``. This is the durable replacement for hand-curating
    DASHBOARD_DEP_TESTS: a freshly-added dashboard test is auto-routed to the dashboard job, never false-redded.
    """
    if not dashboard_modules:
        return []
    # -rE keeps it quiet; pytest prints "ERROR <file>" lines + a per-file ImportError block we scan together.
    collect_cmd = "python -m pytest tests/ --collect-only -q -p no:cacheprovider 2>&1"
    result = run([*fp_docker(worktree), "sh", "-c", collect_cmd], timeout=300)
    output = result.stdout + "\n" + result.stderr
    errored_files = {
        line.split()[1]
        for line in output.splitlines()
        if line.startswith("ERROR ") and len(line.split()) >= 2 and line.split()[1].startswith("tests/")
    }
    if not errored_files:
        return []
    missing_modules = {match.group(1).split(".")[0] for match in _MODULE_NOT_FOUND_RE.finditer(output)}
    is_dashboard_cause = bool(missing_modules & dashboard_modules)
    detected: list[str] = []
    for path in sorted(errored_files):
        # Confirm per-file (so a non-dashboard collection error in ANOTHER file isn't misattributed): collect
        # this file alone and check its error names a dashboard module.
        alone = run(
            [
                *fp_docker(worktree),
                "sh",
                "-c",
                f"python -m pytest {path} --collect-only -q -p no:cacheprovider 2>&1",
            ],
            timeout=120,
        )
        file_missing = {
            m.group(1).split(".")[0] for m in _MODULE_NOT_FOUND_RE.finditer(alone.stdout + alone.stderr)
        }
        if file_missing & dashboard_modules:
            detected.append(path)
    if detected:
        logger.info("auto-detected dashboard-dep tests (routed to the dashboard job): %s", detected)
    elif is_dashboard_cause:
        logger.info("dashboard module missing in full collect but no single file confirmed it (contention)")
    return detected


def _augment_with_autodetected_dashboard_tests(worktree: str, policy: TestEnvPolicy) -> TestEnvPolicy:
    """Return ``policy`` with any auto-detected dashboard-dep tests merged into ``dashboard_dep_tests`` (so the
    fp job --ignores them, the dashboard job runs them, and the audit treats them as known). Union with the
    static list; if auto-detect finds nothing new, the policy is returned unchanged."""
    detected = _autodetect_dashboard_tests(worktree, _dashboard_dep_modules(worktree))
    new_tests = tuple(t for t in detected if t not in policy.dashboard_dep_tests)
    if not new_tests:
        return policy
    return replace(policy, dashboard_dep_tests=policy.dashboard_dep_tests + new_tests)


# Back-compat default (the daemon's own constants); _audit_coverage uses the per-checkout policy instead.
_KNOWN_COLLECTION_ERRORS = _DEFAULT_POLICY.known_collection_errors


def _audit_coverage(worktree: str, policy: TestEnvPolicy) -> list[str]:
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
    candidates = sorted(errored - policy.known_collection_errors)
    if not candidates:
        return []
    # A candidate "blind spot" could be a contention artifact (a --collect-only that partially completed
    # under heavy box load). Re-collect EACH candidate ALONE (serial, no load) to confirm it genuinely errors
    # before flagging it RED — a transient must not false-red a clean PR.
    confirmed = [path for path in candidates if _file_errors_at_collection(worktree, path)]
    if confirmed != candidates:
        logger.info(
            "audit: %s errored in the full collect but re-collected clean alone (contention) — not flagged",
            sorted(set(candidates) - set(confirmed)),
        )
    return confirmed


def _file_errors_at_collection(worktree: str, test_path: str) -> bool:
    """True iff this single file ERRORs at collection when collected ALONE in the bare fp env (the isolated
    re-confirm — strips out load-induced partial-collection artifacts)."""
    collect_cmd = f"python -m pytest {test_path} --collect-only -q -p no:cacheprovider"
    result = run([*fp_docker(worktree), "sh", "-c", collect_cmd], timeout=120)
    return result.returncode != 0 and "ERROR" in result.stdout


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


def _should_auto_merge(passed: bool, tier: Tier, auto_merge_enabled: bool, labels: list[str]) -> bool:
    """Pure auto-merge predicate (so the safety decision is unit-testable). A PR auto-merges iff the gate is
    GREEN, scope is TIER-1 (fp-neutral + safe), the daemon is in auto-merge mode (NOT grade-only), and no
    ``no-auto`` hold label is present. TIER-2 / red / grade-only / held → never."""
    return passed and tier is Tier.AUTO and auto_merge_enabled and NO_AUTO_LABEL not in labels


def grade_pr(pr: OpenPR, auto_merge_enabled: bool) -> None:
    """Grade one PR: run the suite, classify scope, post status + comment + label, maybe auto-merge."""
    logger.info("grading PR #%s @ %s (%s)", pr.number, pr.head_sha[:9], pr.head_ref)
    post_status(pr.head_sha, "pending", "fp suite running")

    # Make sure the PR head SHA is present locally before we try to check it out (the branch may be new).
    run(["git", "fetch", "origin", pr.head_ref], cwd=REPO_DIR)

    with workdir(prefix="ci-wt-") as worktree:
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

    if _should_auto_merge(passed, scope.tier, auto_merge_enabled, pr.labels):
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
    with workdir(prefix="ci-base-") as worktree:
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

    _warn_if_unstable_workdir()
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
