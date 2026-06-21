"""Continuous AUTO-DEPLOY daemon — keep every docker in sync with merged main (CD Phase-3, all services).

docs/AUTO_DEPLOY.md. Ben's directive: "dockers pick up new code upon merge in a way that doesn't break
anything, have a queue, deploy in batches." This daemon closes the "merged ≠ live" gap (the live fc was 31
commits behind) for ALL services, not just the dashboard.

FLOW per poll:
  1. observe a new ``origin/main`` SHA since the last one we processed;
  2. map its merge's changed paths → affected services + tier (``deploy_scope.affected_services``);
  3. ENQUEUE one entry per affected service (``deploy_queue``);
  4. drain the queue's RIPE auto-batch (coalesced per service to the newest SHA) and DEPLOY each TIER-1 service
     by name (FF the live tree once, then ``compose up -d --no-deps --build <svc>`` — the proven #368/#382
     pattern). TIER-2 / fc-surface entries are LEFT on the queue for the coordinated relaunch.

SAFETY (fail-closed, enforced here):
  * GOLDEN RULE — fc is NEVER deployed here. ``feature-computer`` is TIER-2 by ``deploy_scope`` and is in a
    belt-and-suspenders ``_FORBIDDEN`` set; it is relaunched ONLY by ``ops/nightly_relaunch.sh`` at the
    coordinated window. A fc/fingerprint merge just sits batched until that window (Ben-gated).
  * NEVER ``docker kill``/``restart``; only ``compose up -d --no-deps --build <safe-svc>``.
  * NEVER ``docker kill --filter ancestor=fp-dev``.
  * the live tree is FF'd to origin/main ONLY for a TIER-1 deploy (it carries no fc rebuild — fc keeps running
    its pinned bind-mount until the relaunch). A crypto-canary contention pre-check guards box load.
  * ``--dry-run`` (default until Ben arms) classifies + prints the exact plan and deploys NOTHING.

Run::
    python -m ops.auto_deploy --once --dry-run     # process the latest merge, print the plan, deploy nothing
    python -m ops.auto_deploy --poll 60 --dry-run  # daemon, dry-run
    python -m ops.auto_deploy --poll 60            # ARMED daemon (Ben's click): really redeploys TIER-1 svcs
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import subprocess
import sys
import time

from ops.deploy_queue import DeployEntry, claim_batch, enqueue
from ops.deploy_scope import SERVICE_REGISTRY, DeployTier, affected_services, deploy_command

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s auto-deploy %(message)s")
logger = logging.getLogger("auto_deploy")

# The fc-mounted live tree: FF'd then `compose up` for a TIER-1 service. fc itself keeps its pinned bind-mount.
LIVE_TREE = os.environ.get("CI_LIVE_TREE", "/home/ben/quant-fp")
STATE_FILE = os.path.expanduser(os.environ.get("CI_AUTO_DEPLOY_STATE", "~/.quant-ops/auto_deploy_state"))
SYSTEM_LOG = os.path.expanduser("~/.quant-ops/SYSTEM_LOG.md")
# Containers this daemon will NEVER touch — belt-and-suspenders on top of deploy_scope's TIER-2 classification.
_FORBIDDEN = frozenset(
    {
        "feature-computer",
        "fc",
        "ingestor",
        "executor",
        "crypto-capture",
        "quant-timescaledb-1",
        "quant-mongo",
    }
)
# Box-load ceiling: skip a deploy build if the 1-min load is already above this (don't starve live capture).
MAX_LOAD_FOR_DEPLOY = float(os.environ.get("CI_DEPLOY_MAX_LOAD", "40"))


def run(cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    logger.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True, check=False)


def _origin_main_sha() -> str:
    run(["git", "fetch", "origin", "main"], cwd=LIVE_TREE)
    return run(["git", "rev-parse", "origin/main"], cwd=LIVE_TREE).stdout.strip()


def _merge_changed_paths(old_sha: str, new_sha: str) -> list[str]:
    result = run(["git", "diff", "--name-only", old_sha, new_sha], cwd=LIVE_TREE)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _read_state() -> str:
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE) as handle:
            return handle.read().strip()
    return ""


def _write_state(sha: str) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as handle:
        handle.write(sha)


def _box_load() -> float:
    return os.getloadavg()[0]


def observe_and_enqueue() -> str | None:
    """Detect a new main SHA, enqueue its affected services. Returns the new SHA if it advanced, else None."""
    new_sha = _origin_main_sha()
    last_sha = _read_state()
    if not last_sha:
        _write_state(new_sha)
        logger.info("baseline set to %s (no enqueue on first run)", new_sha[:9])
        return None
    if last_sha == new_sha:
        return None

    paths = _merge_changed_paths(last_sha, new_sha)
    plan = affected_services(paths)
    logger.info(
        "merge %s..%s (%d paths): %s", last_sha[:9], new_sha[:9], len(paths), "; ".join(plan.reasons)
    )

    entries: list[DeployEntry] = []
    for service in plan.auto:
        entries.append(DeployEntry.new(service, DeployTier.AUTO.value, new_sha, len(paths)))
    for service in plan.coordinated:
        entries.append(DeployEntry.new(service, DeployTier.COORDINATED.value, new_sha, len(paths)))
    enqueue(entries)
    if plan.unknown_paths:
        logger.warning("ESCALATE — unknown container paths (no mapped service): %s", plan.unknown_paths)
    _write_state(new_sha)  # consumed; the queue now owns the deploy intent
    return new_sha


def deploy_service(service: str, dry_run: bool) -> bool:
    """FF the live tree + rebuild/recreate ONE TIER-1 service by name. Returns True on a healthy deploy."""
    if service in _FORBIDDEN or SERVICE_REGISTRY[service].tier is DeployTier.COORDINATED:
        logger.error(
            "REFUSING to auto-deploy '%s' (forbidden / TIER-2) — that's the relaunch window", service
        )
        return False
    cmd = deploy_command(service, LIVE_TREE)
    if dry_run:
        logger.info("[dry-run] would FF %s to origin/main + run: %s", LIVE_TREE, " ".join(cmd))
        return True

    load = _box_load()
    if load > MAX_LOAD_FOR_DEPLOY:
        logger.warning(
            "box load %.1f > %.1f — deferring '%s' deploy (re-enqueued next tick)",
            load,
            MAX_LOAD_FOR_DEPLOY,
            service,
        )
        return False

    ff = run(["git", "pull", "--ff-only", "origin", "main"], cwd=LIVE_TREE)
    if ff.returncode != 0:
        logger.error("live-tree FF failed (not fast-forwardable?): %s", ff.stderr.strip())
        return False

    logger.info("deploying TIER-1 '%s' (rebuild + recreate by name)", service)
    up = run(cmd, cwd=LIVE_TREE, timeout=900)
    if up.returncode != 0:
        logger.error("deploy '%s' failed: %s", service, up.stderr.strip()[-500:])
        return False

    state = run(["docker", "ps", "--filter", f"name={service}", "--format", "{{.Status}}"])
    healthy = "up" in state.stdout.lower()
    logger.info("post-deploy '%s' status: %s (healthy=%s)", service, state.stdout.strip(), healthy)
    if healthy:
        _record_deploy(service)
    return healthy


def _record_deploy(service: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    line = f"\n### {stamp} (auto-deploy) — REDEPLOYED TIER-1 '{service}' to origin/main (rebuild-by-name). fc untouched.\n"
    if os.path.exists(SYSTEM_LOG):
        with open(SYSTEM_LOG, "a") as handle:
            handle.write(line)
    logger.info("recorded auto-deploy of '%s'", service)


def apply_batch(dry_run: bool) -> None:
    """Drain the ripe auto-batch + deploy each TIER-1 service; report the deferred coordinated batch."""
    auto_batch, coordinated = claim_batch()
    if coordinated:
        logger.info(
            "BATCHED for the coordinated relaunch (Ben-gated, NOT deployed here): %s",
            [f"{e.service}@{e.sha[:9]}" for e in coordinated],
        )
    if not auto_batch:
        return
    logger.info("auto-deploy batch (%d TIER-1 svc): %s", len(auto_batch), [e.service for e in auto_batch])
    failed: list[DeployEntry] = []
    for entry in auto_batch:
        if not deploy_service(entry.service, dry_run):
            failed.append(entry)
    if failed and not dry_run:
        # Re-enqueue failures (e.g. load-deferred) so the next tick retries them.
        enqueue(failed)
        logger.info(
            "re-enqueued %d failed/deferred deploys for retry: %s", len(failed), [e.service for e in failed]
        )


def tick(dry_run: bool) -> None:
    observe_and_enqueue()
    apply_batch(dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous auto-deploy daemon (CD Phase-3, all services)")
    parser.add_argument(
        "--once", action="store_true", help="one tick (observe+enqueue+apply ripe), then exit"
    )
    parser.add_argument("--poll", type=int, default=60, help="daemon poll interval seconds")
    parser.add_argument("--dry-run", action="store_true", help="classify + print the plan; deploy nothing")
    args = parser.parse_args()

    if args.once:
        tick(args.dry_run)
        return 0

    logger.info("auto-deploy daemon: polling every %ss (dry_run=%s)", args.poll, args.dry_run)
    while True:
        tick(args.dry_run)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
