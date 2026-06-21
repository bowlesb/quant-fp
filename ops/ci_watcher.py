"""Self-hosted CI watcher — the merge gate (docs/CONTINUOUS_DEPLOY.md Phase 1-2).

A box-local daemon (no cloud GitHub Actions — the fp test env is the local ``fp-dev`` image + ``/store`` +
DB, which a cloud runner can't replicate). On every open PR against ``main`` whose head SHA it hasn't yet
graded it:

  1. checks the head SHA out into a throwaway git worktree;
  2. runs the FULL ``tests/test_fp_*.py`` suite in an ``fp-dev --rm`` container (env SCRUBBED — never mounts
     ``.env``, so the paper Alpaca creds cannot leak into a CI log). The opt-in latency e2e is run only when
     ``CI_RUN_LATENCY=1`` (it needs ``.env``);
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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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
# The suite the gate runs. Defaults to the FULL fp suite; overridable (CI_SUITE_GLOB) for a focused/smoke
# gate. Whitespace-split so multiple patterns can be passed.
SUITE_GLOB = os.environ.get("CI_SUITE_GLOB", "tests/test_fp_*.py")
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


def fp_docker(worktree: str) -> list[str]:
    """The ``docker run`` prefix for fp-dev jobs over a checkout.

    Runs as the HOST user (so files written into the bind-mounted worktree are owned by us, not root — else
    the throwaway-worktree cleanup hits PermissionError on root-owned ``__pycache__``) with bytecode writing
    OFF, ``HOME=/tmp`` (the non-root user has no home), and NO ``.env`` (env-scrubbed — creds can't leak).
    """
    return [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{worktree}:/app",
        "-w",
        "/app",
        FP_IMAGE,
    ]


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


def run_suite(worktree: str) -> tuple[bool, str]:
    """Run the FULL fp suite in an fp-dev --rm container. Returns (passed, tail_of_output).

    SECURITY: no ``--env-file .env`` and no secret env — the unit suite needs none, and this guarantees the
    paper Alpaca creds never reach a CI log. The container is --rm and bind-mounts the checkout read-write
    only at /app (its own throwaway worktree).
    """
    # Run through ``sh -c`` so the container shell expands the ``test_fp_*.py`` glob (pytest itself does not
    # glob; passed literally it errors "file or directory not found").
    pytest_cmd = f"python -m pytest {SUITE_GLOB} -q -p no:cacheprovider"
    cmd = [*fp_docker(worktree), "sh", "-c", pytest_cmd]
    try:
        result = run(cmd, timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"SUITE TIMED OUT after {SUITE_TIMEOUT_S}s"
    output = (result.stdout + "\n" + result.stderr).strip()
    tail = "\n".join(output.splitlines()[-25:])
    passed = result.returncode == 0
    return passed, tail


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
            passed, tail = run_suite(worktree)
            fp_head = fingerprint_in(worktree)
            fp_base = _origin_main_fingerprint()
            scope = classify(paths, fp_base, fp_head)
        finally:
            run(["git", "worktree", "remove", "--force", worktree], cwd=REPO_DIR)

    tier_str = "TIER-1 (auto)" if scope.tier is Tier.AUTO else "TIER-2 (gated)"
    state = "success" if passed else "failure"
    summary = f"fp suite {'GREEN' if passed else 'RED'} — {tier_str}"
    post_status(pr.head_sha, state, summary)

    body = _comment_body(passed, tail, scope, len(paths))
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


def _comment_body(passed: bool, tail: str, scope: ScopeResult, n_paths: int) -> str:
    """Render the sticky CI comment."""
    status_line = "✅ **fp suite GREEN**" if passed else "❌ **fp suite RED**"
    tier = scope.tier.value
    reasons = "\n".join(f"- {reason}" for reason in scope.reasons)
    lines = [
        f"## CI — `{STATUS_CONTEXT}`",
        "",
        status_line,
        f"**Scope:** `{tier}` ({n_paths} changed paths)",
        "",
        "**Why:**",
        reasons,
    ]
    if not passed:
        lines += [
            "",
            "<details><summary>suite output (tail)</summary>",
            "",
            "```",
            tail,
            "```",
            "</details>",
        ]
    if tier == Tier.AUTO.value and passed:
        lines += ["", "_TIER-1 + green → eligible for auto-merge._"]
    elif tier == Tier.GATED.value:
        lines += ["", "_TIER-2 → gated to the Lead's controlled relaunch window; will NOT auto-merge._"]
    return "\n".join(lines)


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
