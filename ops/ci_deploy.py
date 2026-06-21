"""Auto-deploy watcher — Phase 3 (docs/CONTINUOUS_DEPLOY.md).

On a new merge to ``main``, deploy ONLY a single TIER-1 safe container, never the live trading hot path.
The flow:

  1. detect a new ``origin/main`` SHA since last deploy;
  2. compute the changed paths of that merge and classify scope (ci_scope) + resolve the single deploy
     target container (ci_scope.deploy_target). A non-TIER-1 merge, OR a merge with no single safe target,
     is SKIPPED (logged) — it is the Lead's controlled relaunch window, not ours;
  3. fast-forward the LIVE tree (a SEPARATE checkout, never this worktree) and restart ONLY that container
     via ``docker compose up -d --no-deps <svc>``;
  4. verify the container is healthy/running, then append to READINESS.md + SYSTEM_LOG.md.

HARD INVARIANTS (fail-closed, enforced here):
  * the deploy target comes ONLY from ci_scope.DEPLOY_TARGETS — fc / strategies / crypto can NEVER be named
    (a change touching them is TIER-2 by DANGER_PATTERNS and never reaches step 3);
  * we NEVER ``docker restart``/``start`` fc — only ``compose up -d --no-deps <safe-svc>`` for the mapped
    container. fc is relaunched only by ops/nightly_relaunch.sh at the controlled window;
  * we NEVER ``docker kill --filter ancestor=fp-dev`` (that would kill fc + every sandbox).

Run::

    python -m ops.ci_deploy --once             # deploy the latest main merge if TIER-1, then exit
    python -m ops.ci_deploy --poll 60           # daemon
    python -m ops.ci_deploy --dry-run --once    # classify + print the plan, restart NOTHING
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import subprocess
import sys
import time

from ops.ci_scope import Tier, classify, deploy_target

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s ci-deploy %(message)s",
)
logger = logging.getLogger("ci_deploy")

# The LIVE tree the running containers build from. Deploys FF this tree then restart the safe container.
LIVE_TREE = os.environ.get("CI_LIVE_TREE", "/home/ben/quant-fp")
FP_IMAGE = os.environ.get("CI_FP_IMAGE", "fp-dev")
READINESS = os.path.expanduser("~/.quant-ops/READINESS.md")
SYSTEM_LOG = os.path.expanduser("~/.quant-ops/SYSTEM_LOG.md")
# Containers we will NEVER touch here, belt-and-suspenders on top of ci_scope's path denylist.
FORBIDDEN_SERVICES = frozenset(
    {
        "fc",
        "ingestor",
        "executor",
        "smoke-strategy",
        "reversion-strategy",
        "overnight-beta-strategy",
        "crypto-capture",
        "news-capture",
    }
)


def run(cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    logger.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True, check=False)


def fingerprint_at(tree: str, ref: str) -> int:
    """Fingerprint of a ref, computed in a throwaway worktree of ``tree`` in the fp-dev image."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ci-dep-") as worktree:
        run(["git", "worktree", "add", "--detach", "--force", worktree, ref], cwd=tree)
        try:
            result = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{worktree}:/app",
                    "-w",
                    "/app",
                    FP_IMAGE,
                    "python",
                    "-c",
                    "from quantlib.bus.schema import BusSchema; print(BusSchema.from_registry().fingerprint)",
                ],
                timeout=180,
            )
        finally:
            run(["git", "worktree", "remove", "--force", worktree], cwd=tree)
    if result.returncode != 0:
        raise RuntimeError(f"fingerprint compute failed at {ref}: {result.stderr.strip()[-300:]}")
    return int(result.stdout.strip().splitlines()[-1])


def merge_changed_paths(tree: str, old_sha: str, new_sha: str) -> list[str]:
    """Changed paths between the previously-deployed SHA and the new main SHA."""
    result = run(["git", "diff", "--name-only", old_sha, new_sha], cwd=tree)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def deploy_safe_container(service: str, dry_run: bool) -> bool:
    """Restart ONLY ``service`` via compose. Returns True on a healthy restart (or dry-run plan)."""
    if service in FORBIDDEN_SERVICES:
        # Should be impossible (ci_scope already gated these). Fail-closed regardless.
        logger.error("REFUSING to auto-deploy forbidden service '%s' — escalate to Lead", service)
        return False
    if dry_run:
        logger.info("[dry-run] would: docker compose up -d --no-deps %s (in %s)", service, LIVE_TREE)
        return True

    ff = run(["git", "pull", "--ff-only", "origin", "main"], cwd=LIVE_TREE)
    if ff.returncode != 0:
        logger.error("live-tree FF failed (not fast-forwardable?): %s", ff.stderr.strip())
        return False

    logger.info("restarting safe container '%s' via compose --no-deps", service)
    up = run(
        ["docker", "compose", "up", "-d", "--no-deps", "--build", service],
        cwd=LIVE_TREE,
        timeout=600,
    )
    if up.returncode != 0:
        logger.error("compose up '%s' failed: %s", service, up.stderr.strip()[-400:])
        return False

    state = run(
        ["docker", "compose", "ps", "--format", "{{.Service}} {{.State}}", service],
        cwd=LIVE_TREE,
    )
    logger.info("post-deploy state: %s", state.stdout.strip())
    return "running" in state.stdout.lower() or "up" in state.stdout.lower()


def record_deploy(service: str, new_sha: str, paths: list[str]) -> None:
    """Append a one-line audit trail to SYSTEM_LOG (and a freshness note)."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    line = (
        f"\n### {stamp} (ci-deploy) — AUTO-DEPLOYED TIER-1 '{service}' @ {new_sha[:9]} "
        f"({len(paths)} paths) via compose --no-deps. fc/strategies untouched.\n"
    )
    if os.path.exists(SYSTEM_LOG):
        with open(SYSTEM_LOG, "a") as handle:
            handle.write(line)
    logger.info("recorded deploy of '%s' @ %s", service, new_sha[:9])


def deploy_once(state_file: str, dry_run: bool) -> bool:
    """Deploy the latest main merge if it is TIER-1 with a single safe target. Returns True if it deployed."""
    run(["git", "fetch", "origin", "main"], cwd=LIVE_TREE)
    new_sha = run(["git", "rev-parse", "origin/main"], cwd=LIVE_TREE).stdout.strip()
    last_sha = ""
    if os.path.exists(state_file):
        with open(state_file) as handle:
            last_sha = handle.read().strip()
    if not last_sha:
        # First run: record the current SHA as the baseline; don't deploy history.
        _write_state(state_file, new_sha)
        logger.info("baseline set to %s (no deploy on first run)", new_sha[:9])
        return False
    if last_sha == new_sha:
        return False

    paths = merge_changed_paths(LIVE_TREE, last_sha, new_sha)
    fp_old = fingerprint_at(LIVE_TREE, last_sha)
    fp_new = fingerprint_at(LIVE_TREE, new_sha)
    scope = classify(paths, fp_old, fp_new)
    target = deploy_target(paths)

    if scope.tier is not Tier.AUTO:
        logger.info(
            "merge %s is %s — SKIP auto-deploy (Lead's window). reasons=%s",
            new_sha[:9],
            scope.tier.value,
            scope.reasons,
        )
        _write_state(state_file, new_sha)  # consumed; the Lead deploys it manually
        return False
    if target is None:
        logger.info(
            "merge %s TIER-1 but no single safe deploy target (docs/test/ops-only or multi-svc) "
            "— nothing to restart, marking consumed",
            new_sha[:9],
        )
        _write_state(state_file, new_sha)
        return False

    logger.info("AUTO-DEPLOY: merge %s → TIER-1 container '%s'", new_sha[:9], target)
    ok = deploy_safe_container(target, dry_run)
    if ok and not dry_run:
        record_deploy(target, new_sha, paths)
        _write_state(state_file, new_sha)
    elif dry_run:
        logger.info("[dry-run] not advancing state")
    else:
        logger.error(
            "deploy of '%s' did NOT verify healthy — leaving state at %s for retry/escalation",
            target,
            last_sha[:9],
        )
    return ok


def _write_state(state_file: str, sha: str) -> None:
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w") as handle:
        handle.write(sha)


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-deploy watcher for TIER-1 safe containers")
    parser.add_argument("--once", action="store_true", help="deploy latest main merge if TIER-1, then exit")
    parser.add_argument("--poll", type=int, default=60, help="daemon poll interval seconds")
    parser.add_argument("--dry-run", action="store_true", help="classify + print the plan; restart nothing")
    parser.add_argument(
        "--state-file",
        default=os.path.expanduser("~/.quant-ops/ci_deploy_state"),
        help="file tracking the last-deployed main SHA",
    )
    args = parser.parse_args()

    if args.once:
        deploy_once(args.state_file, args.dry_run)
        return 0

    logger.info("ci-deploy daemon: polling every %ss (dry_run=%s)", args.poll, args.dry_run)
    while True:
        deploy_once(args.state_file, args.dry_run)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
